from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from monitor.alpaca import AlpacaClient
from monitor.config import Settings, get_settings
from monitor.engine import MonitorEngine
from monitor.models import Alert, MonitorStatus, Quote, Rule, RulesConfig
from monitor.slack import SlackClient
from monitor.store import Store

logger = logging.getLogger("personaltrader.api")
engine: Optional[MonitorEngine] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    store = Store(settings.data_dir)
    alpaca = AlpacaClient(settings)
    slack = SlackClient(settings)
    monitor = MonitorEngine(settings, alpaca, slack, store)
    app.state.engine = monitor
    global engine
    engine = monitor

    task = asyncio.create_task(monitor.run_forever())
    logger.info("API listening and monitor loop running")
    try:
        yield
    finally:
        monitor.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await alpaca.close()
        await slack.close()
        engine = None


app = FastAPI(title="PersonalTrader Monitor", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_engine() -> MonitorEngine:
    current = engine or getattr(app.state, "engine", None)
    if current is None:
        raise HTTPException(status_code=503, detail="Monitor is not running")
    return current


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status", response_model=MonitorStatus)
async def status() -> MonitorStatus:
    return get_engine().status()


@app.get("/quotes", response_model=list[Quote])
async def quotes() -> list[Quote]:
    monitor = get_engine()
    if not monitor.quotes:
        await monitor.poll_once()
    return sorted(monitor.quotes.values(), key=lambda item: item.symbol)


@app.post("/poll", response_model=list[Alert])
async def poll_now() -> list[Alert]:
    try:
        return await get_engine().poll_once()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/rules", response_model=RulesConfig)
async def get_rules() -> RulesConfig:
    try:
        return get_engine().load_config()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/rules", response_model=RulesConfig)
async def replace_rules(config: RulesConfig) -> RulesConfig:
    return get_engine().save_config(config)


@app.put("/rules/{rule_id}", response_model=RulesConfig)
async def upsert_rule(rule_id: str, rule: Rule) -> RulesConfig:
    if rule.id != rule_id:
        rule = rule.model_copy(update={"id": rule_id})
    return get_engine().upsert_rule(rule)


@app.delete("/rules/{rule_id}", response_model=RulesConfig)
async def delete_rule(rule_id: str) -> RulesConfig:
    return get_engine().delete_rule(rule_id)


@app.get("/alerts", response_model=list[Alert])
async def alerts(limit: int = 50) -> list[Alert]:
    return get_engine().store.recent_alerts(limit=limit)


@app.post("/alerts/test")
async def test_slack() -> dict[str, str]:
    monitor = get_engine()
    try:
        await monitor.slack.send_text("PersonalTrader test: Slack is connected.")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "sent"}


def run(settings: Optional[Settings] = None) -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    current = settings or get_settings()
    uvicorn.run(
        "monitor.api:app",
        host=current.monitor_host,
        port=current.monitor_port,
        reload=False,
    )
