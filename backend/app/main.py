from datetime import datetime, timedelta, timezone

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

engine = create_engine(settings.database_url, pool_pre_ping=True)
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
    status: Mapped[str] = mapped_column(String(20))  # success / failed
    total_rows: Mapped[int] = mapped_column(Integer)
    imported_count: Mapped[int] = mapped_column(Integer)
    raw_text: Mapped[str] = mapped_column(Text)
    error: Mapped[str] = mapped_column(String(500))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class ImportIn(BaseModel):
    text: str = Field(max_length=100000)


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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可操作")
    return user


def parse_import_lines(text: str) -> list[dict]:
    """服务端解析粘贴文本：一行一条「测点名 分隔符 浓度」，跳过空行。"""
    rows: list[dict] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        # 支持半角逗号、全角逗号、制表符分隔；都没有时按空白分成「测点 浓度」
        normalized = line.replace("，", ",").replace("\t", ",")
        if "," in normalized:
            site_part, _, value = normalized.partition(",")
        else:
            parts = normalized.split()
            if len(parts) >= 2:
                site_part, value = " ".join(parts[:-1]), parts[-1]
            else:
                site_part, value = (parts[0] if parts else ""), ""
        site = site_part.strip()
        value = value.strip()
        error = ""
        ch4 = None
        level = ""
        note = ""
        if not site:
            error = "测点名不能为空"
        elif not value:
            error = "浓度不能为空"
        else:
            try:
                ch4 = float(value)
            except ValueError:
                error = "浓度必须是数字"
            else:
                level, note = classify(ch4)
        rows.append(
            {
                "line_no": line_no,
                "site": site,
                "ch4_pct": ch4,
                "level": level,
                "note": note,
                "error": error,
            }
        )
    return rows


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
    await push_alert(payload)
    return payload


@app.post("/api/imports/preview")
def preview_import(body: ImportIn, user: dict = Depends(require_writer)):
    """试算：只返回每行将产生的状态，不写库、不推送。"""
    rows = parse_import_lines(body.text)
    if not rows:
        raise HTTPException(status_code=400, detail="没有可导入的行")
    return {
        "total": len(rows),
        "alarms": sum(1 for r in rows if r["level"] == "报警"),
        "ok": all(r["error"] == "" for r in rows),
        "rows": rows,
    }


@app.post("/api/imports/confirm", status_code=201)
async def confirm_import(body: ImportIn, user: dict = Depends(require_writer)):
    """确认：服务端重新解析校验，整批事务入库；任一行失败则整批不入库并留失败记录。"""
    rows = parse_import_lines(body.text)
    if not rows:
        raise HTTPException(status_code=400, detail="没有可导入的行")

    bad = [r for r in rows if r["error"]]
    now = datetime.now(timezone.utc)
    if bad:
        detail = "；".join(f"第{r['line_no']}行：{r['error']}" for r in bad)
        db = SessionLocal()
        try:
            db.add(
                ImportBatch(
                    status="failed",
                    total_rows=len(rows),
                    imported_count=0,
                    raw_text=body.text,
                    error=detail[:500],
                    created_by=user["username"],
                    created_at=now,
                )
            )
            db.commit()
        finally:
            db.close()
        raise HTTPException(status_code=400, detail=f"导入失败，整批未入库：{detail}")

    db = SessionLocal()
    created: list[Reading] = []
    try:
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
        batch = ImportBatch(
            status="success",
            total_rows=len(rows),
            imported_count=len(rows),
            raw_text=body.text,
            error="",
            created_by=user["username"],
            created_at=now,
        )
        db.add(batch)
        db.commit()
        for row in created:
            db.refresh(row)
        db.refresh(batch)
        batch_id = batch.id
        alarm_payloads = [
            {"id": row.id, "site": row.site, "ch4_pct": row.ch4_pct, "level": row.level, "note": row.note}
            for row in created
            if row.level == "报警"
        ]
    finally:
        db.close()

    for payload in alarm_payloads:
        await push_alert(payload)
    return {"batch_id": batch_id, "imported": len(created), "alarms": len(alarm_payloads)}


@app.get("/api/imports")
def list_imports(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        batches = db.query(ImportBatch).order_by(ImportBatch.id.desc()).limit(50).all()
        return [
            {
                "id": b.id,
                "status": b.status,
                "total_rows": b.total_rows,
                "imported_count": b.imported_count,
                "error": b.error,
                "created_by": b.created_by,
                "created_at": b.created_at.isoformat(),
            }
            for b in batches
        ]
    finally:
        db.close()


async def push_alert(payload: dict) -> None:
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
