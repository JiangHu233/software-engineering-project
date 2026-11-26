"""可复现的合成数据集（TASK-S0-01，AV-01 口径）。

口径固定（禁止修改，任务卡"禁止修改"清单）：
    2 个自习室 × 30 个座位、3 名学生、1 名管理员。

设计要点
--------
1. 数据规模是**常量**，任何改动都会破坏 AV-01 的可复现性，因此集中定义在
   ``DATASET_SPEC`` 里，调用方只能读，不能拼参数改规模。
2. 数据集不依附于任何具体数据库文件：``build_dataset(app)`` 只接受一个已经指向
   独立临时库的 app 实例，绝不触碰 instance/studyroom.db。
3. 幂等：重复调用不会产生重复房间/座位/账号，便于在同一库上跑多轮用例。
4. 座位编号在房间内唯一（u3_seat_room_label），房间名全局唯一。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from studyroom.extensions import db
from studyroom.models import Room, Seat, User, ZoneDictionary

# ---------------------------------------------------------------- 规格常量

ROOMS_COUNT = 2
SEATS_PER_ROOM = 30
STUDENTS_COUNT = 3
ADMINS_COUNT = 1

# 开放时间：8:00-22:00，覆盖测试常用的 10:00/11:00/13:00 时段
OPEN_HOUR = 8
CLOSE_HOUR = 22

# 学生账号：stu1/stu2/stu3，密码统一 password1（与既有 conftest 保持一致）
STUDENT_USERNAMES = tuple(f"stu{i}" for i in range(1, STUDENTS_COUNT + 1))
STUDENT_PASSWORD = "password1"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"

# 区域字典：座位 zone 必须取自 ZoneDictionary（Q-06 裁决 B）
ZONE_LABELS = ("A区", "B区", "静音区", "靠窗区")


@dataclass(frozen=True)
class DatasetSpec:
    """数据集规格。frozen 保证运行时不可被测试意外改写。"""

    rooms: int = ROOMS_COUNT
    seats_per_room: int = SEATS_PER_ROOM
    students: int = STUDENTS_COUNT
    admins: int = ADMINS_COUNT
    open_hour: int = OPEN_HOUR
    close_hour: int = CLOSE_HOUR

    @property
    def total_seats(self) -> int:
        return self.rooms * self.seats_per_room


DATASET_SPEC = DatasetSpec()


@dataclass
class Dataset:
    """一次构建的结果，测试用它拿到 id，避免再次按名字查库。"""

    room_ids: list[int] = field(default_factory=list)
    seat_ids_by_room: dict[int, list[int]] = field(default_factory=dict)
    student_ids: list[int] = field(default_factory=list)
    admin_id: int = 0

    @property
    def all_seat_ids(self) -> list[int]:
        out: list[int] = []
        for room_id in self.room_ids:
            out.extend(self.seat_ids_by_room[room_id])
        return out

    def room_of_seat(self, seat_id: int) -> int:
        for room_id, seats in self.seat_ids_by_room.items():
            if seat_id in seats:
                return room_id
        raise KeyError(f"座位 {seat_id} 不属于本数据集")


# ---------------------------------------------------------------- 构建


def _ensure_zones() -> None:
    """补齐区域字典；已存在的不动（_seed_base 可能已建过）。"""
    existing = {z.label for z in ZoneDictionary.query.all()}
    for i, label in enumerate(ZONE_LABELS, start=1):
        if label not in existing:
            db.session.add(ZoneDictionary(code=f"Z{i}", label=label, active=True))


def _ensure_rooms(spec: DatasetSpec) -> list[Room]:
    """建 spec.rooms 个房间，每间 spec.seats_per_room 个座位。幂等。"""
    rooms: list[Room] = []
    for r_idx in range(1, spec.rooms + 1):
        name = f"合成自习室{r_idx}"
        room = Room.query.filter_by(name=name).first()
        if room is None:
            room = Room(name=name, location=f"合成楼 {r_idx}F",
                        open_hour=spec.open_hour, close_hour=spec.close_hour,
                        active=True)
            db.session.add(room)
            db.session.flush()  # 拿到 room.id
        rooms.append(room)
        db.session.flush()

        have = Seat.query.filter_by(room_id=room.id).count()
        for s_idx in range(have + 1, spec.seats_per_room + 1):
            zone = ZONE_LABELS[(s_idx - 1) % len(ZONE_LABELS)]
            seat = Seat(room_id=room.id, label=f"S{s_idx:02d}", zone=zone,
                        state="AVAILABLE")
            seat.features = ["电源"] if s_idx % 2 else ["靠窗", "电源"]
            db.session.add(seat)
        db.session.flush()
    return rooms


def _ensure_users(spec: DatasetSpec) -> tuple[list[User], User]:
    students: list[User] = []
    for i in range(1, spec.students + 1):
        username = STUDENT_USERNAMES[i - 1]
        user = User.query.filter_by(username=username).first()
        if user is None:
            user = User(username=username, display_name=f"合成学生{i}", role="STU")
            user.set_password(STUDENT_PASSWORD)
            db.session.add(user)
            db.session.flush()
        students.append(user)

    admin = User.query.filter_by(username=ADMIN_USERNAME).first()
    if admin is None:
        admin = User(username=ADMIN_USERNAME, display_name="合成管理员", role="ADM")
        admin.set_password(ADMIN_PASSWORD)
        db.session.add(admin)
        db.session.flush()
    return students, admin


def build_dataset(app, spec: DatasetSpec = DATASET_SPEC) -> Dataset:
    """在当前 app 的独立库上构建合成数据集，返回各实体 id。

    调用方必须先进入 app_context，且该 app 的数据库必须是临时文件库。
    函数末尾 commit 一次，保证数据对后续请求可见。
    """
    rooms = _ensure_rooms(spec)
    students, admin = _ensure_users(spec)
    _ensure_zones()

    db.session.commit()

    result = Dataset()
    for room in rooms:
        room = db.session.get(Room, room.id)
        seat_ids = [s.id for s in Seat.query.filter_by(room_id=room.id)
                    .order_by(Seat.label).all()]
        result.room_ids.append(room.id)
        result.seat_ids_by_room[room.id] = seat_ids
    result.student_ids = [u.id for u in students]
    result.admin_id = admin.id
    return result


def verify_dataset(app, spec: DatasetSpec = DATASET_SPEC) -> dict:
    """自检：返回实际规模，与规格不符时抛 AssertionError。用于 TC-E2E-00。"""
    rooms = Room.query.filter(Room.name.like("合成自习室%")).all()
    if len(rooms) != spec.rooms:
        raise AssertionError(f"房间数 {len(rooms)} != {spec.rooms}")
    seat_total = 0
    per_room = {}
    for room in rooms:
        n = Seat.query.filter_by(room_id=room.id).count()
        per_room[room.id] = n
        seat_total += n
        if n != spec.seats_per_room:
            raise AssertionError(f"房间 {room.id} 座位数 {n} != {spec.seats_per_room}")
    students = User.query.filter(User.username.in_(STUDENT_USERNAMES)).count()
    if students != spec.students:
        raise AssertionError(f"学生数 {students} != {spec.students}")
    admins = User.query.filter_by(role="ADM").count()
    if admins < spec.admins:
        raise AssertionError(f"管理员数 {admins} < {spec.admins}")
    return {
        "rooms": len(rooms),
        "seatsTotal": seat_total,
        "seatsPerRoom": per_room,
        "students": students,
        "admins": admins,
    }


def pick_seat(dataset: Dataset, index: int) -> int:
    """按全局序号取座位 id（跨房间连续编号），越界抛 IndexError。

    并发用例每轮要独占一个新座位，用 index 递增即可。
    """
    seats = dataset.all_seat_ids
    if index < 0 or index >= len(seats):
        raise IndexError(f"座位序号 {index} 超出范围 0..{len(seats) - 1}")
    return seats[index]


def slot_tomorrow(hour: int, length_hours: int = 1) -> tuple[datetime, datetime]:
    """生成明天某整点开始的时段，避开"过去时间"与"预约窗口"边界。"""
    if not (0 <= hour <= 23):
        raise ValueError(f"hour 必须在 0..23：{hour}")
    base = (datetime.now() + timedelta(days=1)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    return base, base + timedelta(hours=length_hours)
