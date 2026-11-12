from __future__ import annotations

import os
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "login"


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), unique=True, nullable=False)
    display_name = db.Column(db.String(60), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(12), nullable=False, default="student")
    blacklist_until = db.Column(db.DateTime)
    blacklist_entries = db.Column(db.Integer, nullable=False, default=0)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_blacklisted(self) -> bool:
        return bool(self.blacklist_until and self.blacklist_until > datetime.now())


class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    location = db.Column(db.String(120), nullable=False)
    open_hour = db.Column(db.Integer, nullable=False, default=8)
    close_hour = db.Column(db.Integer, nullable=False, default=22)
    active = db.Column(db.Boolean, nullable=False, default=True)
    seats = db.relationship("Seat", backref="room", cascade="all, delete-orphan")


class Seat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False)
    label = db.Column(db.String(20), nullable=False)
    features = db.Column(db.String(120), nullable=False, default="靠窗 · 电源")
    state = db.Column(db.String(16), nullable=False, default="available")
    __table_args__ = (db.UniqueConstraint("room_id", "label"),)


class Reservation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    seat_id = db.Column(db.Integer, db.ForeignKey("seat.id"), nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False, index=True)
    ends_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="booked", index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    user = db.relationship("User", backref="reservations")
    seat = db.relationship("Seat", backref="reservations")
    __table_args__ = (db.Index("ix_seat_time", "seat_id", "starts_at", "ends_at"),)


class Violation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    reservation_id = db.Column(db.Integer, db.ForeignKey("reservation.id"), nullable=False)
    kind = db.Column(db.String(50), nullable=False)
    reason = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    operator_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    user = db.relationship("User", foreign_keys=[user_id], backref="violations")
    operator = db.relationship("User", foreign_keys=[operator_id])
    reservation = db.relationship("Reservation", backref="violation")


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "local-demo-only-change-before-sharing"),
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{Path(app.instance_path) / 'studyroom.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 15}},
        SEMESTER_START="2026-09-01",
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    db.init_app(app)
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    def admin_required(fn):
        @wraps(fn)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role != "admin":
                abort(403)
            return fn(*args, **kwargs)
        return wrapped

    def record_violation(reservation: Reservation, kind: str, reason: str,
                         operator_id: int | None = None) -> None:
        if Violation.query.filter_by(reservation_id=reservation.id).first():
            return
        db.session.add(Violation(user_id=reservation.user_id, reservation_id=reservation.id,
                                 kind=kind, reason=reason, operator_id=operator_id))
        reservation.status = "violation"
        db.session.flush()
        term_start = datetime.strptime(app.config["SEMESTER_START"], "%Y-%m-%d")
        score = Violation.query.filter(
            Violation.user_id == reservation.user_id,
            Violation.created_at >= term_start,
        ).count()
        user = db.session.get(User, reservation.user_id)
        if score >= 3 and not user.is_blacklisted:
            user.blacklist_entries += 1
            user.blacklist_until = datetime.now() + timedelta(days=min(7 * user.blacklist_entries, 30))

    def process_due_reservations() -> None:
        now = datetime.now()
        for reservation in Reservation.query.filter(
            Reservation.status.in_(["booked", "checked_in"])
        ).all():
            if reservation.status == "booked" and now > reservation.starts_at + timedelta(minutes=15):
                record_violation(reservation, "超时未签到", "预约开始后15分钟仍未签到")
            elif reservation.status == "checked_in" and now > reservation.ends_at + timedelta(minutes=15):
                record_violation(reservation, "超时未签退", "预约结束后15分钟仍未签退")
        db.session.commit()

    @app.before_request
    def run_business_clock():
        if request.endpoint not in {"static", "login", "health"}:
            process_due_reservations()

    def reservation_conflict(seat_id: int, starts_at: datetime, ends_at: datetime) -> bool:
        return Reservation.query.filter(
            Reservation.seat_id == seat_id,
            Reservation.status.in_(["booked", "checked_in"]),
            Reservation.starts_at < ends_at,
            Reservation.ends_at > starts_at,
        ).first() is not None

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("home"))
        if request.method == "POST":
            user = User.query.filter_by(username=request.form.get("username", "").strip()).first()
            if user and user.check_password(request.form.get("password", "")):
                login_user(user)
                return redirect(url_for("home"))
            flash("账号或密码不正确。", "error")
        return render_template("login.html")

    @app.post("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def home():
        rooms = Room.query.filter_by(active=True).order_by(Room.id).all()
        selected_date = request.args.get("date", datetime.now().date().isoformat())
        try:
            day = datetime.strptime(selected_date, "%Y-%m-%d").date()
        except ValueError:
            day = datetime.now().date()
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        reservations = Reservation.query.filter(
            Reservation.starts_at < end, Reservation.ends_at > start,
            Reservation.status.in_(["booked", "checked_in"]),
        ).all()
        booked_by_seat: dict[int, list[Reservation]] = {}
        for reservation in reservations:
            booked_by_seat.setdefault(reservation.seat_id, []).append(reservation)
        mine = Reservation.query.filter_by(user_id=current_user.id).order_by(Reservation.starts_at.desc()).limit(20).all()
        return render_template("index.html", rooms=rooms, day=day, booked_by_seat=booked_by_seat,
                               mine=mine, now=datetime.now(), timedelta=timedelta,
                               violations=Violation.query.filter_by(user_id=current_user.id).order_by(Violation.created_at.desc()).limit(10).all())

    @app.post("/reservations")
    @login_required
    def create_reservation():
        if current_user.role != "student":
            abort(403)
        if current_user.is_blacklisted:
            flash("你当前处于预约限制期，暂不能创建新预约。", "error")
            return redirect(url_for("home"))
        try:
            seat_id = int(request.form.get("seat_id", ""))
            starts_at = datetime.fromisoformat(request.form.get("starts_at", ""))
            ends_at = datetime.fromisoformat(request.form.get("ends_at", ""))
        except (TypeError, ValueError):
            flash("请选择有效的座位和预约时间。", "error")
            return redirect(url_for("home"))
        now = datetime.now()
        if starts_at < now or ends_at <= starts_at or starts_at.minute or starts_at.second or ends_at.minute or ends_at.second:
            flash("预约时间必须为未来整点，结束时间须晚于开始时间。", "error")
            return redirect(url_for("home"))
        if starts_at > now + timedelta(days=7):
            flash("仅可预约未来7天内的时段。", "error")
            return redirect(url_for("home"))
        seat = db.session.get(Seat, seat_id)
        if not seat or seat.state != "available" or not seat.room.active:
            flash("该座位当前不可预约。", "error")
            return redirect(url_for("home"))
        if starts_at.hour < seat.room.open_hour or ends_at.hour > seat.room.close_hour:
            flash("预约时段超出自习室开放时间。", "error")
            return redirect(url_for("home"))
        if Reservation.query.filter(
            Reservation.user_id == current_user.id,
            Reservation.status.in_(["booked", "checked_in"]),
            Reservation.starts_at < ends_at, Reservation.ends_at > starts_at,
        ).first():
            flash("你的其他预约与该时段重叠。", "error")
            return redirect(url_for("home"))
        active_count = Reservation.query.filter(
            Reservation.user_id == current_user.id,
            Reservation.status.in_(["booked", "checked_in"]),
        ).count()
        if active_count >= 2:
            flash("同时最多持有2个有效预约。", "error")
            return redirect(url_for("home"))
        try:
            # Serialize writes on SQLite so simultaneous requests cannot both claim one seat.
            db.session.rollback()
            db.session.execute(text("BEGIN IMMEDIATE"))
            locked_active_count = Reservation.query.filter(
                Reservation.user_id == current_user.id,
                Reservation.status.in_(["booked", "checked_in"]),
            ).count()
            if locked_active_count >= 2 or Reservation.query.filter(
                Reservation.user_id == current_user.id,
                Reservation.status.in_(["booked", "checked_in"]),
                Reservation.starts_at < ends_at, Reservation.ends_at > starts_at,
            ).first():
                db.session.rollback()
                flash("你的预约刚刚发生变化或已达到有效预约上限，请刷新后重试。", "error")
                return redirect(url_for("home"))
            if reservation_conflict(seat.id, starts_at, ends_at):
                db.session.rollback()
                flash("该座位刚刚被其他人预约，请刷新后重试。", "error")
                return redirect(url_for("home"))
            db.session.add(Reservation(user_id=current_user.id, seat_id=seat.id,
                                       starts_at=starts_at, ends_at=ends_at))
            db.session.commit()
            flash("预约成功，记得在开始后15分钟内签到。", "success")
        except Exception:
            db.session.rollback()
            flash("预约未能保存，请重试。", "error")
        return redirect(url_for("home"))

    @app.post("/reservations/<int:reservation_id>/cancel")
    @login_required
    def cancel_reservation(reservation_id: int):
        reservation = db.get_or_404(Reservation, reservation_id)
        if current_user.role != "admin" and reservation.user_id != current_user.id:
            abort(403)
        if reservation.status != "booked" or reservation.starts_at - datetime.now() < timedelta(minutes=30):
            flash("仅可在预约开始前至少30分钟取消未开始的预约。", "error")
        else:
            reservation.status = "cancelled"
            db.session.commit()
            flash("预约已取消。", "success")
        return redirect(url_for("home"))

    @app.post("/reservations/<int:reservation_id>/check-in")
    @login_required
    def check_in(reservation_id: int):
        reservation = db.get_or_404(Reservation, reservation_id)
        if reservation.user_id != current_user.id or current_user.role != "student":
            abort(403)
        now = datetime.now()
        if reservation.status != "booked" or not (reservation.starts_at <= now <= reservation.starts_at + timedelta(minutes=15)):
            flash("当前不在签到时间窗内（预约开始至开始后15分钟）。", "error")
        else:
            reservation.status = "checked_in"
            db.session.commit()
            flash("签到成功，祝你学习愉快。", "success")
        return redirect(url_for("home"))

    @app.post("/reservations/<int:reservation_id>/check-out")
    @login_required
    def check_out(reservation_id: int):
        reservation = db.get_or_404(Reservation, reservation_id)
        if reservation.user_id != current_user.id or current_user.role != "student":
            abort(403)
        now = datetime.now()
        if reservation.status != "checked_in" or now > reservation.ends_at:
            flash("当前无法签退，请在预约结束前操作。", "error")
        else:
            if now <= reservation.ends_at - timedelta(minutes=30):
                record_violation(reservation, "提前离开", "提前签退30分钟及以上")
                flash("已签退；根据当前规则，此次提前30分钟及以上记为违约。", "error")
            else:
                reservation.status = "completed"
                flash("签退成功，座位时段已释放。", "success")
            db.session.commit()
        return redirect(url_for("home"))

    @app.get("/admin")
    @admin_required
    def admin_dashboard():
        total_seats = Seat.query.join(Room).filter(Room.active.is_(True)).count()
        active_bookings = Reservation.query.filter(Reservation.status.in_(["booked", "checked_in"])).count()
        violations = Violation.query.order_by(Violation.created_at.desc()).limit(30).all()
        in_use = Reservation.query.filter_by(status="checked_in").order_by(Reservation.starts_at).all()
        rooms = Room.query.order_by(Room.id).all()
        return render_template("admin.html", rooms=rooms, total_seats=total_seats,
                               active_bookings=active_bookings, violation_count=Violation.query.count(),
                               violations=violations, in_use=in_use)

    @app.post("/admin/rooms")
    @admin_required
    def add_room():
        name = request.form.get("name", "").strip()
        location = request.form.get("location", "").strip()
        if not name or not location:
            flash("自习室名称和位置不能为空。", "error")
        elif Room.query.filter_by(name=name).first():
            flash("自习室名称已存在。", "error")
        else:
            db.session.add(Room(name=name, location=location))
            db.session.commit()
            flash("自习室已创建。", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/rooms/<int:room_id>/seats")
    @admin_required
    def add_seat(room_id: int):
        room = db.get_or_404(Room, room_id)
        label = request.form.get("label", "").strip()
        if not label:
            flash("座位编号不能为空。", "error")
        elif Seat.query.filter_by(room_id=room.id, label=label).first():
            flash("这个房间已有同编号座位。", "error")
        else:
            db.session.add(Seat(room_id=room.id, label=label))
            db.session.commit()
            flash("座位已添加。", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/seats/<int:seat_id>/toggle")
    @admin_required
    def toggle_seat(seat_id: int):
        seat = db.get_or_404(Seat, seat_id)
        if seat.state == "available":
            seat.state = "maintenance"
            upcoming = Reservation.query.filter(
                Reservation.seat_id == seat.id,
                Reservation.status == "booked",
                Reservation.starts_at > datetime.now(),
            ).all()
            for reservation in upcoming:
                reservation.status = "cancelled"
            if upcoming:
                flash(f"座位已停用，已取消 {len(upcoming)} 笔未来预约（本机演示不发送外部通知）。", "success")
            else:
                flash(f"座位 {seat.label} 已停用。", "success")
        else:
            seat.state = "available"
            flash(f"座位 {seat.label} 已恢复开放。", "success")
        db.session.commit()
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/reservations/<int:reservation_id>/violation")
    @admin_required
    def manual_violation(reservation_id: int):
        reservation = db.get_or_404(Reservation, reservation_id)
        reason = request.form.get("reason", "").strip()
        if reservation.status != "checked_in" or not reason:
            flash("仅能对正在使用中的预约登记违约，并必须填写原因。", "error")
        else:
            record_violation(reservation, "现场违规", reason, current_user.id)
            db.session.commit()
            flash("违约已登记并留痕。", "success")
        return redirect(url_for("admin_dashboard"))

    @app.cli.command("init-db")
    def init_db_command():
        db.create_all()
        seed_demo_data()
        print("数据库已初始化；演示账号：admin/admin123、student/student123")

    @app.cli.command("scan-violations")
    def scan_violations_command():
        process_due_reservations()
        print("自动违约扫描完成")

    with app.app_context():
        db.create_all()
        seed_demo_data()
    return app


def seed_demo_data() -> None:
    if not User.query.filter_by(username="admin").first():
        admin = User(username="admin", display_name="自习室管理员", role="admin")
        admin.set_password("admin123")
        student = User(username="student", display_name="演示学生", role="student")
        student.set_password("student123")
        db.session.add_all([admin, student])
    if not Room.query.first():
        room = Room(name="星河自习室", location="图书馆 3F · 东区", open_hour=8, close_hour=22)
        room.seats = [Seat(label=f"A{i:02d}", features="靠窗 · 电源" if i % 2 else "安静区 · 电源") for i in range(1, 13)]
        db.session.add(room)
    db.session.commit()
    if not Reservation.query.first():
        student = User.query.filter_by(username="student").first()
        seat = Seat.query.order_by(Seat.id).first()
        if student and seat:
            yesterday = datetime.now() - timedelta(days=1)
            db.session.add(Reservation(user_id=student.id, seat_id=seat.id,
                                       starts_at=yesterday.replace(hour=10, minute=0, second=0, microsecond=0),
                                       ends_at=yesterday.replace(hour=12, minute=0, second=0, microsecond=0),
                                       status="completed"))
            db.session.commit()


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
