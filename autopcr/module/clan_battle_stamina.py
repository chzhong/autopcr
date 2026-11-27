"""一次清日常中由全局配置和前段体力模块共享的会战策略。"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from typing import Optional

from ..core.apiclient import apiclient
from ..core.pcrclient import pcrclient
from ..db.database import db
from ..model.common import ClanPoint
from ..model.error import AbortError, SkipError
from ..model.modelbase import ResponseBase
from ..model.responses import ClanBattleTopResponse


MAX_CLAN_BATTLE_CP = 900

@dataclass(frozen=True)
class ClanBattleStaminaPolicy:
    """会战体力策略在清日常的会战调整阶段临时放行禅模式。
    减少会战期间刷体力管理负担. 专心出刀或者保持躺平.

    允许刷 CP：会战期间 CP 不足 900 时先做日常扫荡。若仍不足，由已勾选的调整模块补刷。
    防止碎片备战都盈余时，消耗的体力攒不够900CP。

    温存体力：会战前夕午后直接生效；会战期间在 CP 达标后生效。允许领取体力，
    并根据预计出刀时间限制常规扫荡。最终日 CP 达标后放行，但不再保留次日体力。

    防爆体：开启温存后，可在前夕或 CP 达标后扫荡最新图，消耗预计将溢出的自回体。
    """

    allow_cp: bool
    reserve: bool
    attack_time: str
    next_battle: bool
    game_day: datetime

    @property
    def target_time(self) -> datetime:
        # 从下一游戏日 05:00 起算，填写 00:00–04:59 时目标落在该游戏日末尾。
        hour, minute = map(int, self.attack_time.split(':'))
        return self.game_day + timedelta(days=1, minutes=(hour * 60 + minute - 300) % 1440)

    def mode(self, point: Optional[int]) -> Optional[str]:
        if point is not None and point < MAX_CLAN_BATTLE_CP and self.allow_cp:
            return 'cp'
        if point is not None and point >= MAX_CLAN_BATTLE_CP and self.reserve:
            return 'reserve' if self.next_battle else 'release'
        return None

    def reserve_floor(self, now: datetime, maximum: int, recovery_seconds: int) -> int:
        # 用体力上限减去预计自回的体力，不足 1 点舍去。
        seconds = max(0, (self.target_time - now).total_seconds())
        recovery = int(seconds // recovery_seconds)
        return max(0, maximum - recovery)


def is_reserve_eve() -> bool:
    # 与现有“会战前夕午后”一致：游戏日 05:00 起算七小时。
    day = db.get_today_start_time()
    return (apiclient.datetime >= day + timedelta(hours=7)
            and not db.is_clan_battle_time()
            and db.is_clan_battle_time(day + timedelta(days=1)))


class ClanBattleStamina:
    """维护单次清日常的 CP 和停止原因，跨游戏日时停用旧状态。"""

    def __init__(self, client: pcrclient, *, is_daily: bool):
        self.client = client
        self.is_daily = is_daily
        self.game_day = db.get_today_start_time()
        self.policy: Optional[ClanBattleStaminaPolicy] = None
        self.point: Optional[int] = None
        self.stop_reason = ''
        self._top: Optional[ClanBattleTopResponse] = None

    @property
    def active(self) -> bool:
        return (db.get_today_start_time() == self.game_day
                and (db.is_clan_battle_time() or (self.policy is not None and self.policy.reserve and is_reserve_eve())))

    async def get_top(self) -> ClanBattleTopResponse:
        if not self.active or not db.is_clan_battle_time():
            raise SkipError('当前没有公会战或已跨游戏日')
        if self._top is None:
            # 只查询初始 CP，之后由成功扫荡的响应更新 point。
            self._top = await self.client.clan_battle_top()
            self.point = self._top.point
        if self.point is None:
            raise AbortError('未获取到公会战CP')
        return self._top

    async def configure(self, allow_cp: bool, reserve: bool, attack_time: str):
        if not self.is_daily or not (allow_cp or reserve) or db.get_today_start_time() != self.game_day:
            return
        next_battle = db.is_clan_battle_time(self.game_day + timedelta(days=1))
        if db.is_clan_battle_time():
            await self.get_top()
        elif not (reserve and is_reserve_eve()):
            return
        self.policy = ClanBattleStaminaPolicy(
            allow_cp, reserve, attack_time,
            next_battle, self.game_day,
        )

    @property
    def mode(self) -> Optional[str]:
        if self.policy is not None and self.active:
            # 前夕没有当日会战 CP，按时间直接温存；会战中每次按最新 CP 判断。
            return 'reserve' if not db.is_clan_battle_time() else self.policy.mode(self.point)
        return None

    @property
    def recovery_floor(self) -> int:
        """到预计出刀时间靠自回体恰好回满所需的当前体力。"""
        if self.policy is None or not self.active:
            return 0
        data = self.client.data
        return self.policy.reserve_floor(
            apiclient.datetime, db.team_info[data.team_level].max_stamina,
            data.settings.quest.recovery_time,
        )

    @property
    def reserve_floor(self) -> int:
        # 最终日仍可按 recovery_floor 防爆，但常规扫荡没有温存下限。
        return self.recovery_floor if self.mode == 'reserve' else 0

    @property
    def needs_cp(self) -> bool:
        return self.mode == 'cp'

    def adjustment_needed(self, prevent_overflow: bool) -> bool:
        return self.needs_cp or (
            prevent_overflow and self.mode in ('reserve', 'release')
            and self.client.data.stamina > self.recovery_floor
        )

    def adjustment_batch(self, times: int, cost: int) -> int:
        """兜底只补CP或消耗预计溢出的体力，不替代普通需求刷取。"""
        if self.needs_cp:
            return self.limit_batch(times, cost)
        if self.mode not in ('reserve', 'release') or cost <= 0:
            return 0
        return min(times, max(0, self.client.data.stamina - self.recovery_floor) // cost)

    def limit_batch(self, times: int, cost: int) -> int:
        if self.policy is None or not self.active:
            return times
        mode = self.mode
        if mode is None and self.client.is_stamina_consume_not_run():
            self.stop_reason = '体力消耗，不执行'
            return 0
        if not cost:
            return times
        if mode == 'reserve':
            available = max(0, self.client.data.stamina - self.reserve_floor)
            allowed = min(times, available // cost)
            if not allowed:
                self.stop_reason = '已达到会战温存体力'
            return allowed
        if self.point is not None and self.point < MAX_CLAN_BATTLE_CP:
            # 向上取整到预计刷满 CP 的最少场次，剩余扫荡再按新状态判断。
            return min(times, ceil((MAX_CLAN_BATTLE_CP - self.point) / cost))
        return times

    def can_recover_stamina(self) -> bool:
        # 温存只消费已有的多余体力，用完预算就停。
        return self.mode != 'reserve'

    def record_spend(self, stamina: int, response: ResponseBase) -> None:
        if self.point is None or not self.active:
            return
        change = getattr(response, 'clan_point', None)
        if isinstance(change, ClanPoint) and change.after_point is not None:
            self.point = change.after_point
        elif self.point < MAX_CLAN_BATTLE_CP:
            # 按实际关卡成本估算，升级或奖励返还的体力不影响CP。
            self.point = min(MAX_CLAN_BATTLE_CP, self.point + stamina)
