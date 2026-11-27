import unittest
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from autopcr.core.pcrclient import pcrclient
from autopcr.core.apiclient import apiclient
from autopcr.module.clan_battle_stamina import ClanBattleStamina, ClanBattleStaminaPolicy
from autopcr.core.datamgr import datamgr
from autopcr.db.database import db
from autopcr.module.modules import daily_modules
from autopcr.module.modules.autosweep import clan_battle_stamina_adjust, daily_quest_mission
from autopcr.module.modules.clan import clan_battle_knive
from autopcr.module.modules.sweep import explore_exp, explore_mana
from autopcr.module.modules.VIP import role_mission_get
from autopcr.module.modulebase import Module, eResultStatus, tag_stamina_consume
from autopcr.module.modulemgr import ModuleManager
from autopcr.module.modulelistmgr import ModuleListManager
from autopcr.model.common import ClanPoint
from autopcr.model.enums import eMissionStatusType
from autopcr.model.error import SkipError, AbortError
from autopcr.model.responses import ClanInfoResponse


NOW = datetime(2026, 11, 26, 18)
DAY = datetime(2026, 11, 26, 5)
QUESTS = [11001003, 11001002, 11001001]


class Parent:
    id = 'test-clan-stamina'

    def __init__(self, config=None, state=None):
        self.config = config or {}
        self.clan_battle_stamina = state

    def get_config(self, key, default):
        return self.config.get(key, default)


class PolicyTest(unittest.TestCase):
    def test_reserve_example(self):
        policy = ClanBattleStaminaPolicy(True, True, '06:00', True, DAY)
        self.assertEqual(policy.target_time, datetime(2026, 11, 27, 6))
        self.assertEqual(policy.reserve_floor(NOW, 414, 360), 294)
        self.assertEqual(policy.reserve_floor(NOW, 50, 360), 0)

    def test_before_reset_and_midnight(self):
        policy = ClanBattleStaminaPolicy(True, True, '06:00', True, DAY)
        self.assertEqual(policy.reserve_floor(datetime(2026, 11, 27, 2), 414, 360), 374)
        midnight = ClanBattleStaminaPolicy(True, True, '00:00', True, DAY)
        self.assertEqual(midnight.target_time, datetime(2026, 11, 28))

    def test_modes(self):
        for allow in (False, True):
            for reserve in (False, True):
                policy = ClanBattleStaminaPolicy(allow, reserve, '05:00', True, DAY)
                self.assertEqual(policy.mode(899), 'cp' if allow else None)
                self.assertEqual(policy.mode(900), 'reserve' if reserve else None)
        last = ClanBattleStaminaPolicy(False, True, '05:00', False, DAY)
        self.assertIsNone(last.mode(0))
        self.assertEqual(last.mode(900), 'release')


class StaminaIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(apiclient, 'datetime', NOW))
        self.stack.enter_context(patch.object(db, 'get_today_start_time', return_value=DAY))
        self.battle = self.stack.enter_context(patch.object(db, 'is_clan_battle_time', return_value=True))
        self.stack.enter_context(patch.object(type(db), 'team_info', {
            1: SimpleNamespace(max_stamina=414, recover_stamina_count=40),
        }))
        self.stack.enter_context(patch.object(db, 'last_normal_quest', return_value=QUESTS))
        self.stack.enter_context(patch.object(db, 'get_quest_name', side_effect=str))
        self.stack.enter_context(patch.object(type(db), 'quest_info', {
            q: SimpleNamespace(stamina=18, daily_limit=0) for q in QUESTS
        }))
        self.client = pcrclient.__new__(pcrclient)
        self.client._base_keys = {}
        self.client._keys = {}
        self.client.data = SimpleNamespace(
            team_level=1, stamina=588, clan=1,
            get_shop_gold=lambda system: 0,
            recover_stamina_exec_count=0,
            settings=SimpleNamespace(quest=SimpleNamespace(recovery_time=360)),
            quest_dict={q: SimpleNamespace(clear_flg=3) for q in QUESTS},
            get_quest_stamina_half_campaign_times=lambda q: 100,
            is_quest_sweepable=lambda q: True,
        )
        self.state = ClanBattleStamina(self.client, is_daily=True)
        self.state.point = 900
        self.client.clan_battle_top = AsyncMock(side_effect=self.top)
        self.client.quest_skip = AsyncMock(side_effect=self.skip)
        self.client.recover_stamina = AsyncMock(side_effect=self.buy)
        self.client.serialize_reward_summary = AsyncMock(return_value='奖励')

    async def top(self):
        return SimpleNamespace(point=self.state.point)

    async def skip(self, quest, count):
        data = self.client.data
        cost = self.client.quest_stamina_cost(quest) * count
        data.stamina -= cost
        return SimpleNamespace(
            quest_result_list=[], bonus_reward_list=[],
        )

    async def buy(self):
        self.client.data.stamina += 120
        self.client.data.recover_stamina_exec_count += 1

    def policy(self, allow=True, reserve=True, tomorrow=True):
        self.state.policy = ClanBattleStaminaPolicy(
            allow, reserve, '06:00', tomorrow, DAY,
        )

    def module(self, recover=0, overflow=False):
        return clan_battle_stamina_adjust(Parent({
            'clan_battle_adjust_recover': recover,
            'clan_battle_adjust_prevent_overflow': overflow,
        }, state=self.state))

    async def sweep(self, quest, times):
        return await self.module().quest_skip_aware(self.client, quest, times)

    async def test_sweep_respects_floor_and_forced_override(self):
        self.policy()
        self.client._keys['stamina_consume_not_run'] = True
        with self.client.override_config({'stamina_consume_not_run': False}):
            _, clears, stopped = await self.sweep(QUESTS[0], 100)
        self.assertEqual(clears, 16)
        self.assertEqual(self.client.data.stamina, 300)
        self.assertTrue(stopped)
        self.assertEqual(self.state.stop_reason, '已达到会战温存体力')
        self.client.recover_stamina.assert_not_awaited()

    async def test_training_sweeps_ignore_clan_battle_stamina_policy(self):
        self.policy()
        client = self.client
        client._keys['stamina_consume_not_run'] = True
        client.data.stamina = 0
        client.data.get_max_quest_exp = lambda available: 21002001
        client.data.get_max_quest_mana = lambda available: 21001001
        client.data.training_quest_max_count = SimpleNamespace(exp_quest=5, gold_quest=3)
        client.data.training_quest_count = SimpleNamespace(exp_quest=1, gold_quest=1)
        client.training_quest_skip = AsyncMock()

        for point in (890, 900):
            self.state.point = point
            for module_cls, quest, count in ((explore_exp, 21002001, 4), (explore_mana, 21001001, 2)):
                with self.subTest(point=point, module=module_cls.__name__):
                    client.training_quest_skip.reset_mock()
                    module = module_cls(Parent(state=self.state))
                    self.assertNotIn('体力消耗', module.tags)
                    self.assertTrue((await module.do_check(client))[0])
                    await module.do_task(client)
                    client.training_quest_skip.assert_awaited_once_with(quest, count)
                    self.assertEqual(self.state.point, point)
        client.recover_stamina.assert_not_awaited()

    async def test_half_cost_and_receive_more_stamina(self):
        self.policy()
        self.client.data.get_quest_stamina_half_campaign_times = lambda q: 50
        _, clears, _ = await self.sweep(QUESTS[0], 100)
        self.assertEqual(clears, 32)
        self.client.data.stamina += 120
        _, clears, _ = await self.sweep(QUESTS[0], 100)
        self.assertEqual(clears, 14)
        self.assertEqual(self.client.data.stamina, 294)

    async def test_cp_threshold_switches_inside_batch(self):
        self.policy()
        self.state.point = 890
        self.client.data.stamina = 310
        _, clears, stopped = await self.sweep(QUESTS[0], 10)
        self.assertEqual(clears, 1)
        self.assertEqual(self.client.data.stamina, 292)
        self.assertTrue(stopped)

    async def test_cp_and_reserve_limit_two_batches_in_one_request(self):
        self.policy()
        self.state.point = 870
        self.client.data.stamina = 350  # 温存值为294，当前体力最多支持3次扫荡。
        self.client._keys.update(stamina_recover_times=2, quest_skip_remaining=10)
        quest = QUESTS[0]
        _, clears, stopped = await self.sweep(quest, 5)

        self.assertEqual([call.args for call in self.client.quest_skip.await_args_list], [(quest, 2), (quest, 1)])
        self.assertEqual((clears, stopped), (3, True))
        self.assertEqual(self.state.point, 900)
        self.assertEqual(self.client.data.stamina, 296)
        self.assertEqual(self.client.quest_skip_remaining, 7)
        self.client.recover_stamina.assert_not_awaited()

    async def test_cp_threshold_restores_zen(self):
        self.policy(reserve=False)
        self.client._keys['stamina_consume_not_run'] = True
        self.state.point = 890
        _, clears, stopped = await self.sweep(QUESTS[0], 10)
        self.assertEqual(clears, 1)
        self.assertTrue(stopped)
        self.assertTrue(self.client.is_stamina_consume_not_run())

    async def test_extra_two_purchases_and_rotation(self):
        self.state.point = 630
        self.client.data.stamina = 89
        self.client.data.recover_stamina_exec_count = 3
        self.policy()
        module = self.module(recover=2)
        await module.do_task(self.client)
        self.assertEqual(self.state.point, 900)
        self.assertEqual(self.client.data.recover_stamina_exec_count, 5)
        self.assertEqual(self.client.recover_stamina.await_count, 2)
        self.assertEqual([call.args[0] for call in self.client.quest_skip.await_args_list[:3]], QUESTS)
        self.assertFalse(module.is_warn)

    async def test_insufficient_quota_warns(self):
        self.policy()
        self.state.point = 630
        self.client.data.stamina = 89
        module = self.module(recover=1)
        await module.do_task(self.client)
        self.assertLess(self.state.point, 900)
        self.assertTrue(any('体力不足，CP 未到900' in line for line in module.log))

    async def test_reserve_adjust_switch(self):
        self.policy()
        with self.assertRaisesRegex(SkipError, '无需补CP或防爆调整'):
            await self.module().do_task(self.client)
        await self.module(overflow=True).do_task(self.client)
        self.assertEqual(self.client.data.stamina, 300)
        self.client.recover_stamina.assert_not_awaited()

    async def test_last_battle_day_releases_and_prevents_overflow(self):
        self.policy(tomorrow=False)
        self.client._keys.update(stamina_get_not_run=True, stamina_consume_not_run=True)
        self.assertFalse(self.module().is_stamina_get_not_run(self.client))
        self.assertFalse(self.module().is_stamina_consume_not_run(self.client))
        self.assertEqual(self.state.reserve_floor, 0)
        await self.module(overflow=True).do_task(self.client)
        self.assertEqual(self.client.data.stamina, 300)
        self.client.recover_stamina.assert_not_awaited()
        # 常规需求在最终日不受温存值限制。
        _, clears, _ = await self.sweep(QUESTS[0], 10)
        self.assertEqual(clears, 10)

    async def test_no_battle_or_unconfigured_module_does_nothing(self):
        self.battle.return_value = False
        with self.assertRaisesRegex(SkipError, '当前没有有效'):
            await self.module().do_task(self.client)
        self.client.clan_battle_top.assert_not_awaited()
        self.battle.return_value = True
        self.state.point = 100
        self.client._keys['stamina_consume_not_run'] = True
        with self.assertRaisesRegex(SkipError, '当前没有有效'):
            await self.module().do_task(self.client)
        self.client.quest_skip.assert_not_awaited()

    async def test_no_progress_and_game_purchase_limit(self):
        self.policy()
        self.state.point = 100
        self.client.data.stamina = 0
        self.client.data.recover_stamina_exec_count = 40
        module = self.module(recover=40)
        await module.do_task(self.client)
        self.client.recover_stamina.assert_not_awaited()
        self.assertTrue(module.is_warn)
        self.client.data.stamina = 50
        self.client.quest_skip_aware = AsyncMock(return_value=([], 0, False))
        module = self.module()
        await module.do_task(self.client)
        self.assertTrue(module.is_warn)
        self.assertEqual(self.client.quest_skip_aware.await_count, 1)

    async def test_single_module_has_no_automatic_stamina_policy(self):
        state = ClanBattleStamina(self.client, is_daily=False)
        await state.configure(True, True, '05:00')
        self.assertIsNone(state.policy)
        self.client.clan_battle_top.assert_not_awaited()
        order = daily_modules.modules
        self.assertEqual(order[order.index(daily_quest_mission) - 1], clan_battle_stamina_adjust)

    async def test_cp_updates_from_response_including_zero(self):
        for point in (900, 0):
            self.state.record_spend(18, SimpleNamespace(clan_point=ClanPoint(after_point=point)))
            self.assertEqual(self.state.point, point)

    async def test_clan_info_integer_point_does_not_overwrite_cp(self):
        self.state.point = 630
        data = datamgr()
        response = ClanInfoResponse(clan={'detail': {'clan_id': 42}}, clan_point=123)
        handler = SimpleNamespace(request=AsyncMock(return_value=response))
        self.assertIs(await data.request(None, handler), response)
        self.assertEqual(data.clan, 42)
        self.assertEqual(self.state.point, 630)

    async def test_no_sweepable_quests(self):
        self.policy()
        self.state.point = 100
        self.client.data.is_quest_sweepable = lambda q: False
        with self.assertRaisesRegex(AbortError, '均无法扫荡'):
            await self.module(recover=2).do_task(self.client)
        self.client.recover_stamina.assert_not_awaited()

    async def test_no_adaptation_preserves_regular_batch(self):
        self.client._keys['stamina_recover_times'] = 2
        self.client.data.stamina = 0
        _, clears, stopped = await self.client.quest_skip_aware(QUESTS[0], 10)
        self.assertEqual(clears, 10)
        self.assertFalse(stopped)
        self.assertEqual(self.client.recover_stamina.await_count, 2)
        self.client.quest_skip.assert_awaited_once_with(QUESTS[0], 10)

    async def test_quest_budget_preserves_partial_rewards(self):
        self.policy()
        self.state.point = 890
        self.client.data.stamina = 310
        db.quest_info[QUESTS[0]].daily_limit = 3
        self.client.data.quest_dict[QUESTS[0]].daily_clear_count = 0
        self.client.data.quest_dict[QUESTS[0]].daily_recovery_count = 0
        self.client.data.recover_max_time = lambda q: 0
        original_skip = self.skip

        async def reward_skip(quest, count):
            response = await original_skip(quest, count)
            response.bonus_reward_list = ['reward']
            return response

        self.client.quest_skip.side_effect = reward_skip
        rewards, clears, stopped = await self.sweep(QUESTS[0], 3)
        self.assertEqual((rewards, clears, stopped), (['reward'], 1, True))

    async def test_reserve_alone_obeys_zen_until_cp_full(self):
        self.policy(allow=False)
        self.state.point = 890
        self.client._keys.update(stamina_get_not_run=True, stamina_consume_not_run=True)
        self.assertTrue(self.module().is_stamina_get_not_run(self.client))
        self.assertTrue(self.module().is_stamina_consume_not_run(self.client))
        _, clears, stopped = await self.sweep(QUESTS[0], 1)
        self.assertEqual((clears, stopped), (0, True))
        self.client._keys['stamina_consume_not_run'] = False
        await self.sweep(QUESTS[0], 1)
        self.assertEqual(self.state.mode, 'reserve')

    async def test_configure_outside_battle_is_noop(self):
        self.battle.return_value = False
        await self.state.configure(True, True, '05:00')
        self.assertIsNone(self.state.policy)
        self.client.clan_battle_top.assert_not_awaited()

    async def test_unconfigured_state_does_not_pass_sweep_budget(self):
        self.state.point = None
        self.client.quest_skip_aware = AsyncMock(return_value=([], 0, False))
        await self.sweep(QUESTS[0], 1)
        self.client.quest_skip_aware.assert_awaited_once_with(QUESTS[0], 1, False, False)

    async def test_untagged_module_does_not_receive_sweep_budget(self):
        self.policy()
        module = Module(Parent(state=self.state))
        self.client.quest_skip_aware = AsyncMock(return_value=([], 1, False))
        await module.quest_skip_aware(self.client, QUESTS[0], 1)
        self.client.quest_skip_aware.assert_awaited_once_with(QUESTS[0], 1, False, False)

    async def test_configure_after_game_day_rollover_is_noop(self):
        with patch.object(db, 'get_today_start_time', return_value=datetime(2026, 11, 27, 5)):
            await self.state.configure(True, True, '05:00')
        self.assertIsNone(self.state.policy)
        self.client.clan_battle_top.assert_not_awaited()

    async def test_parent_without_optional_state_uses_regular_sweep(self):
        module = Module(SimpleNamespace(id='test-optional-state'))
        self.assertIsNone(module.clan_battle_stamina)
        self.client.quest_skip_aware = AsyncMock(return_value=([], 1, False))
        await module.quest_skip_aware(self.client, QUESTS[0], 1)
        self.client.quest_skip_aware.assert_awaited_once_with(QUESTS[0], 1, False, False)

    def test_global_adaptation_options_are_first(self):
        from autopcr.module.modules.daily import global_config
        self.assertEqual(list(global_config(Parent()).generate_config())[:2], [
            'clan_battle_stamina_options', 'clan_battle_attack_time',
        ])

    def test_grouped_config_migrates_and_explicit_empty_wins(self):
        from autopcr.module.modules.daily import global_config
        for allow in (False, True):
            for reserve in (False, True):
                parent = Parent({'clan_battle_allow_cp': allow, 'clan_battle_reserve_stamina': reserve})
                module = global_config(parent)
                expected = (['允许刷CP'] if allow else []) + (['为次日温存体力'] if reserve else [])
                self.assertEqual(module.get_config('clan_battle_stamina_options'), expected)
                parent.config['clan_battle_stamina_options'] = []
                self.assertEqual(module.get_config('clan_battle_stamina_options'), [])
                config = module.generate_config()['clan_battle_stamina_options']
                self.assertEqual(config['config_type'], 'multi')
                self.assertEqual(config['default'], [])

    async def test_eve_reserves_without_cp_requests(self):
        self.state.point = None
        self.battle.side_effect = lambda when=None: when is not None and when >= DAY.replace(day=27)
        await self.state.configure(False, True, '06:00')
        self.assertEqual(self.state.mode, 'reserve')
        self.client.clan_battle_top.assert_not_awaited()
        self.client._keys.update(stamina_get_not_run=True, stamina_consume_not_run=True)
        self.assertFalse(self.module().is_stamina_get_not_run(self.client))
        await self.sweep(QUESTS[0], 100)
        self.assertEqual(self.client.data.stamina, 300)
        self.client.recover_stamina.assert_not_awaited()
        with self.assertRaisesRegex(SkipError, '无需补CP'):
            await self.module().do_task(self.client)

    async def test_eve_before_noon_and_unrelated_days_do_not_configure(self):
        for tomorrow in (False, True):
            self.battle.side_effect = lambda when=None: tomorrow and when is not None
            with patch.object(apiclient, 'datetime', DAY.replace(hour=11)):
                state = ClanBattleStamina(self.client, is_daily=True)
                await state.configure(True, True, '06:00')
                self.assertIsNone(state.policy)
        self.client.clan_battle_top.assert_not_awaited()

    async def test_cp_fallback_respects_module_checkbox(self):
        self.policy()
        self.state.point = 630
        module = self.module()
        self.assertTrue(self.state.needs_cp)
        self.assertFalse((await module.do_check(self.client))[0])
        module._parent.config[module.key] = True
        self.assertTrue((await module.do_check(self.client))[0])

    async def test_explicit_module_does_not_override_global_policy(self):
        self.state.policy = None
        self.state.point = 630
        self.client.data.stamina = 89
        self.client._keys.update(stamina_get_not_run=True, stamina_consume_not_run=True)
        with self.assertRaisesRegex(SkipError, '当前没有有效'):
            await self.module(recover=2).do_task(self.client)
        self.client.quest_skip.assert_not_awaited()
        self.client.recover_stamina.assert_not_awaited()

    async def test_explicit_cp_module_execution_matrix(self):
        for zen in (False, True):
            for allow in (False, True):
                for reserve in (False, True):
                    for tomorrow in (False, True):
                        for quota in (0, 1, 2):
                            with self.subTest(zen=zen, allow=allow, reserve=reserve, tomorrow=tomorrow, quota=quota):
                                self.policy(allow=allow, reserve=reserve, tomorrow=tomorrow)
                                self.state.point = 630
                                self.client.data.stamina = 89
                                self.client.data.recover_stamina_exec_count = 0
                                self.client._keys['stamina_consume_not_run'] = zen
                                module = self.module(recover=quota)
                                if not allow:
                                    with self.assertRaisesRegex(SkipError, '无需补CP'):
                                        await module.do_task(self.client)
                                    self.assertEqual(self.state.point, 630)
                                    self.assertEqual(self.client.data.stamina, 89)
                                    continue
                                await module.do_task(self.client)
                                self.assertEqual(self.state.point == 900, quota == 2)
                                self.assertEqual(self.client.data.recover_stamina_exec_count, quota)
                                self.assertEqual(module.is_warn, quota < 2)
                                self.assertEqual(self.client.is_stamina_consume_not_run(), zen)

    async def test_daily_strategy_table(self):
        from itertools import product
        for phase, cp, zen, allow, reserve, overflow in product(
                ('ordinary', 'eve', 'battle', 'last'), (630, 900), (False, True),
                (False, True), (False, True), (False, True)):
            with self.subTest(phase=phase, cp=cp, zen=zen, allow=allow, reserve=reserve, overflow=overflow):
                in_battle = phase in ('battle', 'last')
                tomorrow = phase in ('eve', 'battle')
                self.battle.side_effect = lambda when=None: in_battle if when is None else tomorrow
                self.state = ClanBattleStamina(self.client, is_daily=True)
                self.state.point = cp if in_battle else None
                self.client.data.stamina = 588
                self.client.data.recover_stamina_exec_count = 0
                self.client._keys.update(stamina_consume_not_run=zen, stamina_get_not_run=zen)
                queries_before = self.client.clan_battle_top.await_count
                await self.state.configure(allow, reserve, '06:00')
                expected_mode = None
                if phase == 'eve' and reserve:
                    expected_mode = 'reserve'
                elif in_battle:
                    if cp < 900 and allow:
                        expected_mode = 'cp'
                    elif cp == 900 and reserve:
                        expected_mode = 'reserve' if tomorrow else 'release'
                self.assertEqual(self.state.mode, expected_mode)
                module = self.module(overflow=overflow)
                blocked = zen and expected_mode is None
                self.assertEqual(module.is_stamina_get_not_run(self.client), blocked)
                self.assertEqual(module.is_stamina_consume_not_run(self.client), blocked)
                fill = in_battle and cp < 900 and allow
                prevent = overflow and expected_mode in ('reserve', 'release')
                if fill or prevent:
                    await module.do_task(self.client)
                else:
                    with self.assertRaises(SkipError):
                        await module.do_task(self.client)
                expected_stamina = 318 if fill else 588
                # CP刷满后，按新的温存/最终日释放模式决定是否继续防爆。
                if overflow and reserve and (fill or prevent):
                    expected_stamina = 300
                self.assertEqual(self.client.data.stamina, expected_stamina)
                if in_battle:
                    self.assertEqual(self.state.point, 900 if fill else cp)
                else:
                    self.assertIsNone(self.state.point)
                    self.assertEqual(self.client.clan_battle_top.await_count, queries_before)
                self.assertEqual(self.client.is_stamina_consume_not_run(), zen)
                self.assertEqual(self.client.data.recover_stamina_exec_count, 0)

    async def test_morning_cp_transition_and_evening_reserve(self):
        self.policy()
        self.state.point = 890
        self.client.data.stamina = 30
        self.client._keys.update(stamina_consume_not_run=True, stamina_get_not_run=True)
        with patch.object(apiclient, 'datetime', DAY.replace(hour=7)):
            _, clears, stopped = await self.sweep(QUESTS[0], 20)
            self.assertEqual((clears, stopped), (1, True))
            self.assertEqual(self.state.point, 900)
            self.assertEqual(self.client.data.stamina, 12)
            self.assertEqual(self.state.reserve_floor, 184)
        # 傍晚再次领取体力后按当时的温存值继续正常刷取。
        self.client.data.stamina = 588
        _, clears, stopped = await self.sweep(QUESTS[0], 100)
        self.assertEqual((clears, stopped), (16, True))
        self.assertEqual(self.state.reserve_floor, 294)
        self.assertEqual(self.client.data.stamina, 300)
        self.client.recover_stamina.assert_not_awaited()

    async def test_forecast_changes_with_attack_time(self):
        self.state.point = 900
        self.client.data.stamina = 350  # 当前未满，但到06:00会溢出。
        self.policy()
        self.assertTrue(self.state.adjustment_needed(True))
        self.state.policy = ClanBattleStaminaPolicy(True, True, '05:00', True, DAY)
        self.assertEqual(self.state.recovery_floor, 304)
        self.client.data.stamina = 300
        self.assertFalse(self.state.adjustment_needed(True))
        self.state.policy = ClanBattleStaminaPolicy(True, True, '06:00', True, DAY)
        self.assertTrue(self.state.adjustment_needed(True))

    async def test_missing_cp_does_not_enable_or_run_strategy(self):
        self.state.point = None
        with self.assertRaisesRegex(AbortError, '未获取到公会战CP'):
            await self.state.configure(True, True, '06:00')
        self.assertIsNone(self.state.policy)
        with self.assertRaises(SkipError):
            await self.module(recover=40, overflow=True).do_task(self.client)
        self.client.quest_skip.assert_not_awaited()
        self.client.recover_stamina.assert_not_awaited()

    async def test_global_options_only_initialize_cp_when_enabled(self):
        await self.state.configure(False, False, '05:00')
        self.client.clan_battle_top.assert_not_awaited()
        await self.state.configure(True, True, '06:00')
        self.assertEqual(self.state.policy.attack_time, '06:00')
        self.client.clan_battle_top.assert_awaited_once()

    async def test_role_mission_forcing_still_reserves_stamina(self):
        self.policy()
        self.client._keys['stamina_consume_not_run'] = True
        client = self.client

        @tag_stamina_consume
        class Sweep(Module):
            async def do_from(self, client):
                await self.quest_skip_aware(client, QUESTS[0], 100)
                return SimpleNamespace(status=eResultStatus.SUCCESS)

        client.mission_index = AsyncMock(return_value=SimpleNamespace(missions=[SimpleNamespace(
            mission_id=1, clear_num=0, mission_status=eMissionStatusType.NoClear,
        )]))
        with patch.object(type(db), 'VIP_mission', SimpleNamespace(daily_mission_id=1, condition_num=100)):
            module = role_mission_get(Parent(state=self.state))
            module.first_order = [Sweep]
            module.second_order = []
            await module.do_task(client)
        self.assertEqual(client.data.stamina, 300)
        self.assertTrue(any('已达到会战温存体力' in line for line in module.log))
        client.recover_stamina.assert_not_awaited()

    async def test_account_level_purchase_limit(self):
        self.policy()
        db.team_info[1].recover_stamina_count = 4
        self.state.point = 630
        self.client.data.stamina = 89
        self.client.data.recover_stamina_exec_count = 3
        module = self.module(recover=40)
        await module.do_task(self.client)
        self.assertEqual(self.client.data.recover_stamina_exec_count, 4)
        self.assertTrue(module.is_warn)

    async def test_manager_clears_policy_even_on_failure(self):
        client = self.client
        client.activate = AsyncMock()
        client.deactivate = Mock()
        manager = SimpleNamespace(
            id='test-clan-stamina', client=client, config={}, _daily_warnings=[],
            is_clan_battle_forbidden=lambda: False,
        )

        async def fail(client):
            self.assertTrue(manager.clan_battle_stamina.is_daily)
            await manager.clan_battle_stamina.configure(True, True, '06:00')
            raise RuntimeError('test failure')

        module = SimpleNamespace(key='test', do_from=fail)
        with patch.object(db, 'enter_cache_scope', new_callable=AsyncMock), \
                patch.object(db, 'exit_cache_scope', new_callable=AsyncMock):
            with self.assertRaisesRegex(RuntimeError, 'test failure'):
                await ModuleManager.do_task(manager, {}, [module], is_daily=True)
        self.assertIsNone(manager.clan_battle_stamina)
        client.deactivate.assert_called_once()

    async def test_one_top_request_shared_by_daily_modules(self):
        client = self.client
        client.clan_battle_top = pcrclient.clan_battle_top.__get__(client)
        client.request = AsyncMock(return_value=SimpleNamespace(point=630, remaining_count=3, carry_over=[]))
        client.data.stamina = 89
        await self.state.configure(True, True, '06:00')
        await self.module(recover=2).do_task(client)
        self.assertEqual(self.state.point, 900)
        self.assertEqual(client.request.await_count, 1)
        self.assertEqual((await self.state.get_top()).point, 630)

    async def test_daily_scope_ends_after_clan_adjustment_even_if_disabled(self):
        client = self.client
        client.activate = AsyncMock()
        client.deactivate = Mock()
        client.session = SimpleNamespace(_logged=True)
        client.data.ready = True
        client.data.stamina = 300
        for kind in ('normal', 'hard', 'very_hard'):
            setattr(client.data, f'is_{kind}_quest_campaign', lambda: False)
        client._keys.update(stamina_get_not_run=True, stamina_consume_not_run=True)
        client.mission_index = AsyncMock(return_value=SimpleNamespace(missions=[SimpleNamespace(
            mission_id=123, mission_status=eMissionStatusType.NoClear, clear_num=19,
        )]))
        self.state.point = 900
        manager = Parent({
            'clan_battle_stamina_options': ['允许刷CP', '为次日温存体力'],
            'clan_battle_attack_time': '06:00',
            'daily_quest_mission_dusk_only': False,
        })
        manager.client = client
        manager._daily_warnings = []
        manager.is_clan_battle_forbidden = lambda: False

        classes = daily_modules.modules
        last_sweep = classes.index(daily_quest_mission)
        for enabled, zen in ((True, True), (False, True), (True, False), (False, False)):
            with self.subTest(adjustment_enabled=enabled, zen=zen):
                client._keys.update(stamina_get_not_run=zen, stamina_consume_not_run=zen)
                client.data.stamina = 300
                client.quest_skip.reset_mock()
                manager.config[clan_battle_stamina_adjust.__name__] = enabled
                modules = [cls(manager) for cls in classes]
                before = [module for module in modules[:last_sweep] if '体力获取' in module.tags]
                after = [module for module in modules[last_sweep + 1:] if '体力获取' in module.tags]
                self.assertEqual([module.key for module in before], [
                    'mission_receive_first', 'clan_like', 'room_accept_all', 'present_receive',
                ])
                self.assertEqual([module.key for module in after], [
                    'mission_receive_last', 'seasonpass_reward', 'room_upper_all',
                ])
                for module in before + after:
                    module.do_task = AsyncMock()
                    manager.config[module.key] = True
                final = modules[last_sweep]
                final._get_mission_ids = lambda: {123}
                adjust = modules[last_sweep - 1]
                self.assertIs(type(adjust), clan_battle_stamina_adjust)
                ordered = [modules[0], *before, adjust, final, *after]
                with patch.object(db, 'enter_cache_scope', new_callable=AsyncMock), \
                        patch.object(db, 'exit_cache_scope', new_callable=AsyncMock):
                    result = await ModuleManager.do_task(manager, {}, ordered, is_daily=True)
                self.assertIn('无需补CP' if enabled else '功能未启用', result.result[adjust.key].log)
                for module in before:
                    module.do_task.assert_awaited_once_with(client)
                if zen:
                    client.quest_skip.assert_not_awaited()
                    self.assertIn('禅模式下每日关卡任务未完成', result.result[final.key].log)
                    for module in after:
                        module.do_task.assert_not_awaited()
                        self.assertIn('体力获取，不执行', result.result[module.key].log)
                else:
                    client.quest_skip.assert_awaited_once_with(QUESTS[0], 1)
                    self.assertEqual(client.data.stamina, 282)
                    self.assertIn('每日关卡任务已完成', result.result[final.key].log)
                    for module in after:
                        module.do_task.assert_awaited_once_with(client)

    async def test_knife_tool_uses_independent_query(self):
        client = self.client
        client.activate = AsyncMock()
        client.deactivate = Mock()
        client.session = SimpleNamespace(_logged=True)
        client.data.ready = True
        manager = Parent({'clan_battle_stamina_options': ['允许刷CP', '为次日温存体力']})
        manager.client = client
        manager._daily_warnings = []
        manager.is_clan_battle_forbidden = lambda: False
        manager.modules_list = ModuleListManager(manager)
        manager.do_task = ModuleManager.do_task.__get__(manager)
        manager.save_single_result = AsyncMock(side_effect=lambda key, result: result)

        async def query():
            self.assertIsNone(manager.clan_battle_stamina)
            return SimpleNamespace(point=630, remaining_count=3, carry_over=[])

        client.clan_battle_top.side_effect = query
        with patch.object(db, 'enter_cache_scope', new_callable=AsyncMock), \
                patch.object(db, 'exit_cache_scope', new_callable=AsyncMock):
            for in_battle in (True, False):
                self.battle.return_value = in_battle
                result = await ModuleManager.do_from_key(manager, {}, clan_battle_knive.__name__)
                self.assertEqual(result.status, eResultStatus.WARNING)
                self.assertIn('体力点数：630', result.log)
        self.assertEqual(client.clan_battle_top.await_count, 2)

    async def test_top_cache_expires_between_runs_and_game_days(self):
        self.state.point = 630
        await self.state.get_top()
        await self.state.get_top()
        self.client.clan_battle_top.assert_awaited_once()
        next_run = ClanBattleStamina(self.client, is_daily=True)
        await next_run.get_top()
        self.assertEqual(self.client.clan_battle_top.await_count, 2)
        with patch.object(db, 'get_today_start_time', return_value=datetime(2026, 11, 27, 5)):
            with self.assertRaisesRegex(SkipError, '跨游戏日'):
                await next_run.get_top()
            next_run.record_spend(18, SimpleNamespace())
            self.assertEqual(next_run.point, 630)
            self.assertEqual(self.client.clan_battle_top.await_count, 2)

    async def test_non_battle_run_never_creates_or_queries_cp_state(self):
        self.battle.return_value = False
        client = self.client
        client.activate = AsyncMock()
        client.deactivate = Mock()
        manager = SimpleNamespace(
            id='test-clan-stamina', client=client, config={}, _daily_warnings=[],
            is_clan_battle_forbidden=lambda: False,
        )

        async def run(client):
            self.assertIsNone(manager.clan_battle_stamina)
            sweep = clan_battle_stamina_adjust(manager)
            with self.assertRaisesRegex(SkipError, '当前没有公会战'):
                await sweep.do_task(client)
            await sweep.quest_skip_aware(client, QUESTS[0], 1)
            return SimpleNamespace(status=eResultStatus.SUCCESS)

        module = SimpleNamespace(key='test', do_from=run)
        with patch.object(db, 'enter_cache_scope', new_callable=AsyncMock), \
                patch.object(db, 'exit_cache_scope', new_callable=AsyncMock):
            await ModuleManager.do_task(manager, {}, [module], is_daily=True)
        client.clan_battle_top.assert_not_awaited()

    async def test_estimate_uses_discounted_cost_and_caps_at_900(self):
        client = self.client
        self.state.point = 850
        client.data.get_quest_stamina_half_campaign_times = lambda q: 50
        await self.sweep(QUESTS[0], 3)
        self.assertEqual(self.state.point, 877)
        await self.sweep(QUESTS[0], 3)
        self.assertEqual(self.state.point, 900)
        await self.sweep(QUESTS[0], 3)
        self.assertEqual(self.state.point, 900)
        client.clan_battle_top.assert_not_awaited()

    async def test_estimate_does_not_credit_purchase_or_failed_sweep(self):
        client = self.client
        self.state.point = 630
        client.data.stamina = 0
        _, count, stopped = await self.sweep(QUESTS[0], 1)
        self.assertEqual((count, stopped), (0, True))
        self.assertEqual(self.state.point, 630)
        await client.recover_stamina()
        self.assertEqual(self.state.point, 630)
        await self.sweep(QUESTS[0], 1)
        self.assertEqual(self.state.point, 648)
        client.clan_battle_top.assert_not_awaited()

    async def test_server_cp_is_not_counted_twice(self):
        client = self.client
        self.state.point = 630
        original_skip = self.skip

        async def server_skip(quest, count):
            response = await original_skip(quest, count)
            response.clan_point = ClanPoint(after_point=650)
            return response

        client.quest_skip.side_effect = server_skip
        await self.sweep(QUESTS[0], 1)
        self.assertEqual(self.state.point, 650)
        client.clan_battle_top.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
