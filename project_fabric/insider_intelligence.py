"""Official IDX insider-disclosure collector and Telegram formatter.

This module intentionally keeps official disclosures separate from broker-flow
proxies.  A row means an IDX/OJK disclosure was observed; it never claims that
live order-book activity belongs to a named insider.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover
    psycopg = None
    Jsonb = None

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None

from .postgres_backend import configured_database_url


JAKARTA = ZoneInfo("Asia/Jakarta")
FRESH_HOURS = 6
MAX_PDF_BYTES = 10 * 1024 * 1024

_INSIDER_TITLE_MARKERS = (
    "laporan kepemilikan atau setiap perubahan kepemilikan saham",
    "laporan kepemilikan saham",
    "perubahan kepemilikan saham",
    "transaksi saham oleh direksi",
    "transaksi saham oleh komisaris",
    "transaksi saham direksi",
    "transaksi saham komisaris",
    "kepemilikan saham direksi",
    "kepemilikan saham komisaris",
)


@dataclass(frozen=True)
class CollectionResult:
    scope: str
    checked_at: datetime
    matched: int
    stored: int
    skipped_existing: int
    zapi_calls: int
    pdf_errors: int


def _require_db() -> str:
    url = configured_database_url().strip()
    if not url:
        raise RuntimeError("Neon insider cache belum dikonfigurasi")
    if psycopg is None or Jsonb is None:
        raise RuntimeError("Driver psycopg belum tersedia")
    return url


def _connect(url: str | None = None):
    return psycopg.connect(url or _require_db(), connect_timeout=12, autocommit=True)


def migrate(database_url: str | None = None) -> None:
    url = database_url or _require_db()
    statements = (
        "CREATE SCHEMA IF NOT EXISTS project_fabric",
        """
        CREATE TABLE IF NOT EXISTS project_fabric.insider_disclosures (
            disclosure_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            published_at TIMESTAMPTZ,
            transaction_date DATE,
            person_name TEXT,
            person_role TEXT,
            side TEXT NOT NULL DEFAULT 'UNKNOWN'
                CHECK (side IN ('BUY', 'SELL', 'UNKNOWN')),
            shares NUMERIC,
            price NUMERIC,
            transaction_value NUMERIC,
            shares_before NUMERIC,
            shares_after NUMERIC,
            ownership_before_pct NUMERIC,
            ownership_after_pct NUMERIC,
            purpose TEXT,
            announcement_title TEXT NOT NULL,
            source_url TEXT,
            source_provider TEXT NOT NULL DEFAULT 'IDX via ZAPI',
            parse_status TEXT NOT NULL,
            raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            collected_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_insider_disclosures_ticker_date
        ON project_fabric.insider_disclosures (ticker, published_at DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS project_fabric.insider_collection_state (
            scope TEXT PRIMARY KEY,
            checked_at TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL,
            matched INTEGER NOT NULL DEFAULT 0,
            stored INTEGER NOT NULL DEFAULT 0,
            zapi_calls INTEGER NOT NULL DEFAULT 0,
            detail JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """,
    )
    with _connect(url) as con, con.cursor() as cur:
        for statement in statements:
            cur.execute(statement)


def _items(payload: Any) -> list[dict[str, Any]]:
    current = payload
    for _ in range(4):
        if isinstance(current, dict) and isinstance(current.get("data"), dict):
            current = current["data"]
            continue
        break
    if isinstance(current, dict):
        rows = current.get("data") or current.get("items") or []
    elif isinstance(current, list):
        rows = current
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def is_insider_disclosure(item: dict[str, Any]) -> bool:
    title = " ".join(
        str(item.get(key) or "")
        for key in ("title", "Title", "subject", "Subject")
    ).lower()
    return any(marker in title for marker in _INSIDER_TITLE_MARKERS)


def _ticker(item: dict[str, Any]) -> str:
    value = item.get("code") or item.get("Code") or item.get("kodeEmiten") or ""
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())[:12]


def _event_id(item: dict[str, Any]) -> str:
    native = item.get("id") or item.get("Id") or item.get("announcementNo")
    if native:
        return hashlib.sha256(str(native).encode("utf-8")).hexdigest()
    seed = "|".join(
        str(item.get(key) or "")
        for key in ("code", "title", "publishedAt", "createdAt")
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _attachments(item: dict[str, Any]) -> list[dict[str, Any]]:
    rows = item.get("attachments") or item.get("Attachments") or []
    return [row for row in rows if isinstance(row, dict)]


def _source_url(item: dict[str, Any]) -> str | None:
    for attachment in _attachments(item):
        value = attachment.get("pdfUrl") or attachment.get("url") or attachment.get("Url")
        if value:
            return str(value)
    return None


def _extract_embedded_text(item: dict[str, Any]) -> str:
    chunks = []
    for attachment in _attachments(item):
        value = attachment.get("text") or attachment.get("Text")
        if value:
            chunks.append(str(value))
    return "\n".join(chunks)


def _download_pdf_text(url: str, timeout: int = 30) -> str:
    if PdfReader is None:
        raise RuntimeError("pypdf belum tersedia")
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "BOT1-InsiderCollector/1.0"})
    response.raise_for_status()
    if len(response.content) > MAX_PDF_BYTES:
        raise ValueError("PDF disclosure melebihi batas 10 MB")
    reader = PdfReader(io.BytesIO(response.content))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _clean_text(value: str) -> str:
    return re.sub(r"[ \t]+", " ", value.replace("\xa0", " ")).strip()


def _number(value: str | None) -> Decimal | None:
    if not value:
        return None
    digits = re.sub(r"[^0-9-]", "", value)
    if not digits or digits == "-":
        return None
    try:
        return Decimal(digits)
    except Exception:
        return None


def _percentage(value: str | None) -> Decimal | None:
    if not value:
        return None
    match = re.search(r"-?\d+(?:[.,]\d+)?", value)
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", "."))
    except Exception:
        return None


def _capture(text: str, labels: Iterable[str], value_pattern: str = r"([^\n]{1,160})") -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{label_pattern})\s*[:\-]?\s*{value_pattern}", text, re.I)
    return _clean_text(match.group(1)) if match else None


_MONTHS = {
    "januari": 1, "februari": 2, "maret": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "agustus": 8, "september": 9, "oktober": 10, "november": 11,
    "desember": 12,
}


def _date_value(value: str | None) -> date | None:
    if not value:
        return None
    iso = re.search(r"(20\d{2})[-/]([01]?\d)[-/]([0-3]?\d)", value)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    numeric = re.search(r"([0-3]?\d)[-/]([01]?\d)[-/](20\d{2})", value)
    if numeric:
        try:
            return date(int(numeric.group(3)), int(numeric.group(2)), int(numeric.group(1)))
        except ValueError:
            return None
    words = re.search(r"([0-3]?\d)\s+([A-Za-z]+)\s+(20\d{2})", value, re.I)
    if words and words.group(2).lower() in _MONTHS:
        try:
            return date(int(words.group(3)), _MONTHS[words.group(2).lower()], int(words.group(1)))
        except ValueError:
            return None
    return None


def _published_at(item: dict[str, Any]) -> datetime | None:
    value = item.get("publishedAt") or item.get("PublishedDate") or item.get("createdAt")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=JAKARTA)
    except ValueError:
        return None


def parse_disclosure(item: dict[str, Any], text: str = "") -> dict[str, Any]:
    clean = _clean_text(text)
    lower = clean.lower()
    before = _number(_capture(clean, ("jumlah saham sebelum transaksi", "kepemilikan sebelum transaksi")))
    after = _number(_capture(clean, ("jumlah saham setelah transaksi", "kepemilikan setelah transaksi")))
    shares = _number(_capture(clean, (
        "jumlah saham yang dibeli", "jumlah saham yang dijual", "jumlah saham transaksi",
        "jumlah saham yang diperoleh", "jumlah saham yang dialihkan",
    )))
    if shares is None and before is not None and after is not None:
        shares = abs(after - before)

    if re.search(r"\b(penjualan|menjual|dijual|divestasi)\b", lower):
        side = "SELL"
    elif re.search(r"\b(pembelian|membeli|dibeli|akuisisi)\b", lower):
        side = "BUY"
    elif before is not None and after is not None and after != before:
        side = "BUY" if after > before else "SELL"
    else:
        side = "UNKNOWN"

    price = _number(_capture(clean, (
        "harga pembelian per saham", "harga penjualan per saham", "harga transaksi per saham",
        "harga pembelian", "harga penjualan", "harga transaksi",
    )))
    value = shares * price if shares is not None and price is not None else None

    person = _capture(clean, (
        "nama pemilik saham", "nama pemegang saham", "nama direksi/komisaris",
        "nama anggota direksi", "nama anggota dewan komisaris",
    ))
    role = _capture(clean, ("jabatan", "status pihak", "kedudukan"))
    purpose = _capture(clean, ("tujuan transaksi", "tujuan dari transaksi"))
    transaction_date = _date_value(_capture(clean, ("tanggal transaksi", "tanggal pelaksanaan transaksi")))
    before_pct = _percentage(_capture(clean, ("persentase kepemilikan sebelum transaksi",)))
    after_pct = _percentage(_capture(clean, ("persentase kepemilikan setelah transaksi",)))

    structured = side != "UNKNOWN" and any(value is not None for value in (shares, before, after, price))
    return {
        "disclosure_id": _event_id(item),
        "ticker": _ticker(item),
        "published_at": _published_at(item),
        "transaction_date": transaction_date,
        "person_name": person,
        "person_role": role,
        "side": side,
        "shares": shares,
        "price": price,
        "transaction_value": value,
        "shares_before": before,
        "shares_after": after,
        "ownership_before_pct": before_pct,
        "ownership_after_pct": after_pct,
        "purpose": purpose,
        "announcement_title": str(item.get("title") or item.get("Title") or "Disclosure insider"),
        "source_url": _source_url(item),
        "parse_status": "STRUCTURED" if structured else "METADATA_ONLY",
        "raw_json": {"announcement": item, "attachment_text": clean[:40_000]},
    }


def _existing_ids(cur, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    cur.execute(
        "SELECT disclosure_id FROM project_fabric.insider_disclosures WHERE disclosure_id = ANY(%s)",
        (ids,),
    )
    return {row[0] for row in cur.fetchall()}


def _upsert(cur, row: dict[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO project_fabric.insider_disclosures (
            disclosure_id, ticker, published_at, transaction_date, person_name,
            person_role, side, shares, price, transaction_value, shares_before,
            shares_after, ownership_before_pct, ownership_after_pct, purpose,
            announcement_title, source_url, parse_status, raw_json, collected_at
        ) VALUES (
            %(disclosure_id)s, %(ticker)s, %(published_at)s, %(transaction_date)s,
            %(person_name)s, %(person_role)s, %(side)s, %(shares)s, %(price)s,
            %(transaction_value)s, %(shares_before)s, %(shares_after)s,
            %(ownership_before_pct)s, %(ownership_after_pct)s, %(purpose)s,
            %(announcement_title)s, %(source_url)s, %(parse_status)s,
            %(raw_json)s, now()
        )
        ON CONFLICT (disclosure_id) DO UPDATE SET
            ticker = EXCLUDED.ticker,
            published_at = EXCLUDED.published_at,
            transaction_date = EXCLUDED.transaction_date,
            person_name = EXCLUDED.person_name,
            person_role = EXCLUDED.person_role,
            side = EXCLUDED.side,
            shares = EXCLUDED.shares,
            price = EXCLUDED.price,
            transaction_value = EXCLUDED.transaction_value,
            shares_before = EXCLUDED.shares_before,
            shares_after = EXCLUDED.shares_after,
            ownership_before_pct = EXCLUDED.ownership_before_pct,
            ownership_after_pct = EXCLUDED.ownership_after_pct,
            purpose = EXCLUDED.purpose,
            announcement_title = EXCLUDED.announcement_title,
            source_url = EXCLUDED.source_url,
            parse_status = EXCLUDED.parse_status,
            raw_json = EXCLUDED.raw_json,
            collected_at = now()
        """,
        {**row, "raw_json": Jsonb(row["raw_json"])},
    )


def _save_state(cur, result: CollectionResult, status: str = "OK") -> None:
    cur.execute(
        """
        INSERT INTO project_fabric.insider_collection_state (
            scope, checked_at, status, matched, stored, zapi_calls, detail
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (scope) DO UPDATE SET
            checked_at = EXCLUDED.checked_at,
            status = EXCLUDED.status,
            matched = EXCLUDED.matched,
            stored = EXCLUDED.stored,
            zapi_calls = EXCLUDED.zapi_calls,
            detail = EXCLUDED.detail
        """,
        (
            result.scope, result.checked_at, status, result.matched, result.stored,
            result.zapi_calls, Jsonb({"pdf_errors": result.pdf_errors,
                                     "skipped_existing": result.skipped_existing}),
        ),
    )


def ingest_payloads(
    payloads: Iterable[Any],
    *,
    scope: str,
    zapi_calls: int,
    download_pdfs: bool = True,
    database_url: str | None = None,
) -> CollectionResult:
    url = database_url or _require_db()
    migrate(url)
    candidates = [item for payload in payloads for item in _items(payload) if is_insider_disclosure(item)]
    ids = [_event_id(item) for item in candidates]
    stored = 0
    pdf_errors = 0
    checked_at = datetime.now(JAKARTA)

    with _connect(url) as con, con.cursor() as cur:
        existing = _existing_ids(cur, ids)
        for item in candidates:
            if _event_id(item) in existing:
                continue
            text = _extract_embedded_text(item)
            source_url = _source_url(item)
            if not text and download_pdfs and source_url:
                try:
                    text = _download_pdf_text(source_url)
                except Exception:
                    pdf_errors += 1
            row = parse_disclosure(item, text)
            if not row["ticker"]:
                continue
            _upsert(cur, row)
            stored += 1

        result = CollectionResult(
            scope=scope,
            checked_at=checked_at,
            matched=len(candidates),
            stored=stored,
            skipped_existing=sum(1 for event_id in ids if event_id in existing),
            zapi_calls=zapi_calls,
            pdf_errors=pdf_errors,
        )
        _save_state(cur, result)
    return result


def collect_ticker(ticker: str, *, database_url: str | None = None) -> CollectionResult:
    from tracker import fetch_company_announcements

    code = re.sub(r"[^A-Z0-9]", "", ticker.upper())
    if not 2 <= len(code) <= 12:
        raise ValueError("Ticker tidak valid")
    payload = fetch_company_announcements(code, length=100)
    return ingest_payloads(
        [payload], scope=f"TICKER:{code}", zapi_calls=1, database_url=database_url
    )


def collect_global(
    pages: int = 1,
    *,
    database_url: str | None = None,
) -> CollectionResult:
    from tracker import fetch_announcements

    page_count = max(1, min(int(pages), 5))
    payloads = [fetch_announcements(page=page, length=100) for page in range(1, page_count + 1)]
    return ingest_payloads(
        payloads, scope="GLOBAL", zapi_calls=page_count, database_url=database_url
    )


def _state(cur, scope: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT scope, checked_at, status, matched, stored, zapi_calls, detail
        FROM project_fabric.insider_collection_state WHERE scope = %s
        """,
        (scope,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "scope": row[0], "checked_at": row[1], "status": row[2], "matched": row[3],
        "stored": row[4], "zapi_calls": row[5], "detail": row[6] or {},
    }


def read_insider(ticker: str, limit: int = 5, database_url: str | None = None) -> dict[str, Any]:
    url = database_url or _require_db()
    migrate(url)
    code = re.sub(r"[^A-Z0-9]", "", ticker.upper())
    with _connect(url) as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT disclosure_id, ticker, published_at, transaction_date, person_name,
                   person_role, side, shares, price, transaction_value, shares_before,
                   shares_after, ownership_before_pct, ownership_after_pct, purpose,
                   announcement_title, source_url, parse_status, collected_at
            FROM project_fabric.insider_disclosures
            WHERE ticker = %s
            ORDER BY COALESCE(transaction_date, published_at::date) DESC, published_at DESC
            LIMIT %s
            """,
            (code, max(1, min(int(limit), 10))),
        )
        columns = [desc.name for desc in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
        ticker_state = _state(cur, f"TICKER:{code}")
        global_state = _state(cur, "GLOBAL")
    return {"ticker": code, "rows": rows, "state": ticker_state or global_state}


def _fmt_number(value: Any) -> str:
    if value is None:
        return "N/A"
    number = float(value)
    if abs(number) >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f} miliar"
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.2f} juta"
    return f"{number:,.0f}".replace(",", ".")


def format_insider_report(
    ticker: str,
    *,
    refresh: bool = False,
    limit: int = 5,
    database_url: str | None = None,
) -> str:
    code = re.sub(r"[^A-Z0-9]", "", ticker.upper())
    refreshed = None
    try:
        if refresh:
            refreshed = collect_ticker(code, database_url=database_url)
        data = read_insider(code, limit=limit, database_url=database_url)
    except Exception as exc:
        return (
            f"🧑‍💼 INSIDER DISCLOSURE — {code}\n\n"
            f"❌ Cache insider tidak dapat dibaca ({type(exc).__name__}).\n"
            "Data broker/foreign tidak dipakai sebagai pengganti identitas insider."
        )

    state = data.get("state") or {}
    checked = state.get("checked_at")
    checked_text = checked.astimezone(JAKARTA).strftime("%d-%m-%Y %H:%M WIB") if checked else "belum pernah"
    rows = data.get("rows") or []
    lines = [
        f"🧑‍💼 INSIDER DISCLOSURE — {code}",
        "Sumber: IDX/OJK disclosure via ZAPI (bukan order flow live)",
        f"Cache diperiksa: {checked_text}",
    ]
    if refreshed:
        lines.append(
            f"Refresh: {refreshed.stored} baru | ZAPI {refreshed.zapi_calls} | PDF gagal {refreshed.pdf_errors}"
        )
    if not rows:
        lines.extend([
            "",
            "Belum ada disclosure insider yang cocok di cache.",
            f"Gunakan INSIDER REFRESH {code} untuk pemeriksaan khusus emiten (maks. 1 ZAPI).",
        ])
        return "\n".join(lines)

    for index, row in enumerate(rows, 1):
        event_date = row.get("transaction_date") or (
            row["published_at"].date() if row.get("published_at") else None
        )
        side = {"BUY": "🟢 BELI", "SELL": "🔴 JUAL"}.get(row.get("side"), "⚪ BELUM TERBACA")
        lines.extend([
            "",
            f"{index}. {side} | {event_date.isoformat() if event_date else 'Tanggal N/A'}",
            f"Pihak: {row.get('person_name') or 'lihat laporan'}"
            + (f" ({row['person_role']})" if row.get("person_role") else ""),
            f"Saham: {_fmt_number(row.get('shares'))} lembar | Harga: Rp{_fmt_number(row.get('price'))}",
            f"Nilai indikatif: Rp{_fmt_number(row.get('transaction_value'))}",
            f"Status parsing: {row.get('parse_status')}",
        ])
        if row.get("source_url"):
            lines.append(f"Dokumen: {row['source_url']}")

    buys = sum(float(row.get("transaction_value") or 0) for row in rows if row.get("side") == "BUY")
    sells = sum(float(row.get("transaction_value") or 0) for row in rows if row.get("side") == "SELL")
    known = buys or sells
    conclusion = "Akumulasi insider resmi" if buys > sells else "Distribusi insider resmi" if sells > buys else "Belum dapat disimpulkan"
    lines.extend([
        "",
        f"Ringkasan nilai terbaca: beli Rp{_fmt_number(buys)} | jual Rp{_fmt_number(sells)}",
        f"Kesimpulan: {conclusion if known else 'nilai transaksi belum terbaca dari dokumen'}.",
        "Catatan: tanggal publikasi dapat lebih lambat daripada tanggal transaksi.",
    ])
    return "\n".join(lines)

