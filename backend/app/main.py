from datetime import datetime, timedelta, timezone
import re

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    total_lines: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(400))
    raw_text: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class ImportIn(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": username, "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可上报")
    return user


def parse_import_lines(text: str) -> list[dict]:
    """服务端逐行解析“测点名 浓度”，空白行跳过，返回每行的试算结果或错误。"""
    results: list[dict] = []
    for idx, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r\n").rstrip()
        if not line.strip():
            continue
        item: dict = {"line": idx, "site": None, "ch4_pct": None,
                      "level": None, "note": None, "error": None}
        # 只有分隔符加一个值，例如 “,1.2”、“  1.2”、“\t1.2” => 测点名空
        if re.fullmatch(r"[\t,，;；\s]+\S+", line):
            item["error"] = "测点名不能为空"
            results.append(item)
            continue
        m = re.fullmatch(r"(?P<site>.+?)[\t,，;；\s]+(?P<val>\S+)", line)
        if not m:
            item["error"] = "格式应为：测点名 浓度"
            results.append(item)
            continue
        site, value = m.group("site").strip(), m.group("val").strip()
        if not site:
            item["error"] = "测点名不能为空"
            results.append(item)
            continue
        if len(site) > 80:
            item["error"] = "测点名超长"
            results.append(item)
            continue
        try:
            ch4 = float(value)
        except ValueError:
            item["error"] = "浓度不是数字"
            results.append(item)
            continue
        if ch4 != ch4 or ch4 in (float("inf"), float("-inf")):
            item["error"] = "浓度不是有效数字"
            results.append(item)
            continue
        level, note = classify(ch4)
        item.update(site=site, ch4_pct=ch4, level=level, note=note)
        results.append(item)
    return results


async def push(payload: dict) -> None:
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)


def record_failed_batch(text: str, rows: list[dict], username: str) -> None:
    bad = [r for r in rows if r["error"]]
    reason = "；".join(f"第{r['line']}行：{r['error']}" for r in bad)[:400] or "没有可导入的数据行"
    db = SessionLocal()
    try:
        db.add(
            ImportBatch(
                total_lines=len(rows),
                reason=reason,
                raw_text=text,
                created_by=username,
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(Reading).count() == 0:
            now = datetime.now(timezone.utc)
            for site, ch4 in (("东翼-12", 0.35), ("回风巷", 1.4)):
                level, note = classify(ch4)
                db.add(
                    Reading(
                        site=site,
                        ch4_pct=ch4,
                        level=level,
                        note=note,
                        created_by="gasman",
                        created_at=now,
                    )
                )
            db.commit()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        return [
            {
                "id": r.id,
                "site": r.site,
                "ch4_pct": r.ch4_pct,
                "level": r.level,
                "note": r.note,
                "created_by": r.created_by,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    level, note = classify(body.ch4_pct)
    db = SessionLocal()
    try:
        row = Reading(
            site=body.site.strip(),
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        payload = {"id": row.id, "site": row.site, "ch4_pct": row.ch4_pct, "level": row.level, "note": row.note}
    finally:
        db.close()
    await push(payload)
    return payload


@app.post("/api/readings/import/preview")
def import_preview(body: ImportIn, user: dict = Depends(require_writer)):
    """试算：服务端逐行解析，只返回每行将产生的状态，不写库、不推送。"""
    return {"rows": parse_import_lines(body.text)}


@app.post("/api/readings/import/confirm", status_code=201)
async def import_confirm(body: ImportIn, user: dict = Depends(require_writer)):
    """确认：服务端重新解析校验，整批事务入库；任一行失败则整批不入库，仅留失败导入记录。"""
    rows = parse_import_lines(body.text)
    bad = [r for r in rows if r["error"]]
    if not rows or bad:
        record_failed_batch(body.text, rows, user["username"])
        if not rows:
            raise HTTPException(status_code=400, detail="没有可导入的数据行")
        detail = "；".join(f"第{r['line']}行：{r['error']}" for r in bad)
        raise HTTPException(status_code=400, detail=detail)

    now = datetime.now(timezone.utc)
    payloads: list[dict] = []
    db = SessionLocal()
    try:
        created = []
        for r in rows:
            row = Reading(
                site=r["site"],
                ch4_pct=r["ch4_pct"],
                level=r["level"],
                note=r["note"],
                created_by=user["username"],
                created_at=now,
            )
            db.add(row)
            created.append(row)
        db.commit()
        for row in created:
            db.refresh(row)
            payloads.append(
                {"id": row.id, "site": row.site, "ch4_pct": row.ch4_pct,
                 "level": row.level, "note": row.note}
            )
    finally:
        db.close()
    # 报警行按行推送，正常行不打扰
    for payload in payloads:
        if payload["level"] == "报警":
            await push(payload)
    return {"imported": len(payloads), "rows": payloads}


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
