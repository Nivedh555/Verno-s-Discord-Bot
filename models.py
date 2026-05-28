from dataclasses import dataclass


@dataclass
class QueueEntry:
    user_id: int
    social_club_name: str
