"""清日常内的会战体力策略。计算不改变服务器返回的体力库存。"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass(frozen=True)
class ClanBattleStaminaPolicy:
    allow_cp: bool
    reserve: bool
    attack_time: str
    next_battle: bool
    game_day: datetime

    @property
    def target_time(self) -> datetime:
        hour, minute = map(int, self.attack_time.split(':'))
        return self.game_day + timedelta(days=1, minutes=(hour * 60 + minute - 300) % 1440)

    def mode(self, point: Optional[int]) -> Optional[str]:
        if self.reserve and not self.next_battle:
            return 'unlimited'
        if point is not None and point < 900 and self.allow_cp:
            return 'cp'
        if point is not None and point >= 900 and self.reserve:
            return 'reserve'
        return None

    def reserve_floor(self, now: datetime, maximum: int, recovery_seconds: int) -> int:
        seconds = max(0, (self.target_time - now).total_seconds())
        recovery = int(seconds // recovery_seconds)
        return max(0, maximum - recovery)
