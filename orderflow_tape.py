"""Bounded, on-demand order-flow dashboard. Never infer aggression from EOD.

Provider contract: https://zapi.ink/api/finance/pluang?ep=running-trades
Tradebook is a separate snapshot; tape is capped and its CVD starts at zero.
No broker identity, original order IDs or iceberg evidence is supplied.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

WIB = ZoneInfo("Asia/Jakarta")
MAX_PAGES = 3
HELP = "Format: /tape ELTY [60] atau TAPE ELTY [60]. 60 = 60 menit terakhir pada tape tersedia."
ALIASES = {"tape", "/tape", "footprint", "/footprint", "haka", "/haka", "dna", "/dna"}


class TapeReport(str):
    dashboard: bytes | None = None
    metrics: dict | None = None


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def unwrap(payload: Any) -> dict:
    for _ in range(5):
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        else:
            break
    return payload if isinstance(payload, dict) else {}


def rows(data: dict, key: str) -> list[dict]:
    return [r for r in (data.get(key) or []) if isinstance(r, dict)]


def rp(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "N/A"
    sign = "+" if signed and value > 0 else "-" if value < 0 else ""
    value = abs(value)
    for scale, label in ((1e12, "triliun"), (1e9, "miliar"), (1e6, "jt"), (1e3, "rb")):
        if value >= scale:
            return f"{sign}Rp{value / scale:,.2f} {label}"
    return f"{sign}Rp{value:,.0f}"


def trade_time(value: Any) -> tuple[int, str | None] | None:
    text = str(value or "").strip()
    try:
        if "T" in text or (len(text) > 10 and "-" in text):
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo:
                dt = dt.astimezone(WIB)
            return dt.hour * 3600 + dt.minute * 60 + dt.second, dt.date().isoformat()
        parsed = datetime.strptime(text, "%H:%M:%S")
        return parsed.hour * 3600 + parsed.minute * 60 + parsed.second, None
    except ValueError:
        return None


def clock(seconds: int) -> str:
    return f"{seconds//3600:02d}:{seconds%3600//60:02d}:{seconds%60:02d}"


def normalize_trades(items: list[dict]) -> tuple[list[dict], int]:
    result, seen, skipped = [], set(), 0
    for item in items:
        price, lots = number(item.get("price")), number(item.get("lots", item.get("lot")))
        action = str(item.get("action", "")).upper()
        stamp = trade_time(item.get("time"))
        board = str(item.get("board", item.get("market", ""))).upper()
        if board and board not in {"RG", "REGULAR", "REGULER"}:
            skipped += 1
            continue
        if not price or price <= 0 or not lots or lots <= 0 or action not in {"BUY", "SELL"} or stamp is None:
            skipped += 1
            continue
        # Never collapse identical prints without an exchange sequence ID.
        seq = item.get("sequence")
        if seq is not None:
            key = (stamp[1], str(seq))
            if key in seen:
                continue
            seen.add(key)
        result.append({"price": price, "lots": lots, "value": price * lots * 100,
                       "action": action, "seconds": stamp[0], "date": stamp[1],
                       "sequence": number(seq) or 0})
    dates = {r["date"] for r in result if r["date"]}
    if len(dates) > 1:
        newest = max(dates)
        skipped += sum(r["date"] != newest for r in result)
        result = [r for r in result if r["date"] == newest]
    result.sort(key=lambda r: (r["seconds"], r["sequence"]))
    return result, skipped


def aggregate_levels(items: list[dict]) -> list[dict]:
    levels = defaultdict(lambda: {"buy": 0., "sell": 0., "buy_lots": 0., "sell_lots": 0.})
    for r in items:
        side = "buy" if r["action"] == "BUY" else "sell"
        levels[r["price"]][side] += r["value"]
        levels[r["price"]][side + "_lots"] += r["lots"]
    return [{"price": p, **v} for p, v in sorted(levels.items(), reverse=True)]


def book_levels(payload: dict) -> list[dict]:
    data = unwrap(payload)
    source = rows(data, "byPrice") or (rows(data, "items") if data.get("tab", "PRICE") in {"PRICE", "ALL"} else [])
    levels = []
    for r in source:
        p, b, s = number(r.get("price")), number(r.get("buyLots")), number(r.get("sellLots"))
        if p is None or p <= 0 or b is None or s is None or min(b, s) < 0:
            continue
        levels.append({"price": p, "buy": p*b*100, "sell": p*s*100,
                       "buy_lots": b, "sell_lots": s})
    return sorted(levels, key=lambda r: r["price"], reverse=True)


def analyze(ticker: str, payloads: dict, *, window: int | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(WIB)
    trades, skipped = normalize_trades(payloads.get("prints", []))
    if window and trades:
        cutoff = trades[-1]["seconds"] - window*60
        trades = [r for r in trades if r["seconds"] >= cutoff]
    levels = book_levels(payloads.get("tradebook", {})) if not window else []
    basis = "TRADEBOOK SNAPSHOT" if levels else "TAPE PARSIAL"
    if not levels:
        levels = aggregate_levels(trades)
    if not levels or not sum(r["buy"] + r["sell"] for r in levels):
        raise ValueError("Tidak ada transaksi BUY/SELL valid dari provider; HAKA/HAKI tidak dihitung dari EOD.")
    buy, sell = (sum(r[k] for r in levels) for k in ("buy", "sell"))
    bl, sl = (sum(r[k] for r in levels) for k in ("buy_lots", "sell_lots"))
    quote = next(iter(rows(unwrap(payloads.get("summary", {})), "items")), {})
    last, opening = number(quote.get("lastPrice")), number(quote.get("open"))
    vwap_b, vwap_s = buy / (bl*100) if bl else None, sell / (sl*100) if sl else None
    poc = max(levels, key=lambda r: r["buy"] + r["sell"])["price"]
    # POC is value of classified regular-side trades, not all-market volume POC.
    delta, cvd, minutes = 0., [], defaultdict(float)
    for r in trades:
        d = r["value"] * (1 if r["action"] == "BUY" else -1)
        delta += d
        cvd.append(delta)
        minutes[r["seconds"]//60] += d
    ratio = buy/(buy+sell)
    move = ((last / opening - 1)*100) if last and opening and opening > 0 else None
    label = "BELI DOMINAN" if ratio >= .6 else "JUAL DOMINAN" if ratio <= .4 else "CAMPURAN"
    reading = "Perhatikan konfirmasi harga; agresi transaksi tidak mengidentifikasi pemilik/broker."
    if move is not None and ratio >= .65 and abs(move) <= .5:
        reading = "Beli agresif dominan tetapi harga tertahan; kemungkinan diserap penjual pasif, perlu konfirmasi."
    elif move is not None and ratio <= .35 and abs(move) <= .5:
        reading = "Jual agresif dominan tetapi harga bertahan; kemungkinan diserap pembeli pasif, perlu konfirmasi."
    elif move is not None and ratio >= .6 and move > .5:
        label = "MARKUP TERINDIKASI"
    elif move is not None and ratio <= .4 and move < -.5:
        label = "TEKANAN JUAL"
    chart_dates = []
    for r in rows(unwrap(payloads.get("chart", {})), "items"):
        stamp = trade_time(r.get("startTime"))
        if stamp and stamp[1]:
            chart_dates.append(stamp[1])
    explicit_dates = sorted({r["date"] for r in trades if r["date"]})
    session = max(explicit_dates or chart_dates, default=None)
    warnings = list(payloads.get("warnings", []))
    if not explicit_dates:
        warnings.append("Tape hanya jam: tanggal transaksi belum terverifikasi" + (f"; chart terakhir {session}." if session else "."))
    if session and session != now.date().isoformat():
        warnings.append(f"Sesi referensi {session}, bukan data hari ini.")
    warnings += ["Pemisahan nego/crossing tidak dijamin provider; baris nonreguler eksplisit dibuang.",
                 "Tradebook dan tape diambil terpisah; nilai total dapat berbeda. CVD tape mulai dari nol."]
    if skipped:
        warnings.append(f"{skipped} baris tak valid/nonreguler/lintas tanggal dilewati.")
    big = sorted([r for r in trades if r["value"] >= 20e6], key=lambda r: (r["seconds"], r["sequence"]), reverse=True)[:8]
    buckets = [0.]*7
    for r in trades:
        i = next((i for i, hi in enumerate([10e6,50e6,100e6,500e6,1e9,5e9]) if r["value"] < hi), 6)
        buckets[i] += r["value"]
    book = unwrap(payloads.get("orderbook", {}))
    bid_lots = sum(number(r.get("lots")) or 0 for r in rows(book, "bids"))
    ask_lots = sum(number(r.get("lots")) or 0 for r in rows(book, "asks"))
    depth_ratio = bid_lots/(bid_lots+ask_lots) if bid_lots+ask_lots > 0 else None
    return {"bid_depth_ratio": depth_ratio, "best_bid": number(book.get("bestBid")),
            "best_ask": number(book.get("bestAsk")), "ticker": ticker, "fetched": now.isoformat(timespec="seconds"), "session": session,
            "date_verified": bool(explicit_dates), "basis": basis, "window": window,
            "buy": buy, "sell": sell, "net": buy-sell, "ratio": ratio, "vwap_buy": vwap_b,
            "vwap_sell": vwap_s, "last": last, "open": opening, "poc": poc,
            "move": move, "levels": levels, "trades": trades, "cvd": cvd,
            "minutes_buy": sum(v > 0 for v in minutes.values()), "minutes_count": len(minutes),
            "big": big, "buckets": buckets, "label": label, "reading": reading,
            "pages": payloads.get("pages", 0), "has_more": payloads.get("has_more", False),
            "requests": payloads.get("requests", 0), "warnings": warnings}


def fetch_payloads(ticker: str, fetcher=None) -> dict:
    if fetcher is None:
        import tracker
        fetcher = tracker._get
    out = {"prints": [], "warnings": [], "requests": 0, "pages": 0, "has_more": False}
    for key, endpoint, params in (("tradebook", "tradebook", {"code":ticker,"tab":"ALL"}),
                                 ("summary", "summary", {"codes":ticker}),
                                 ("chart", "chart", {"code":ticker}),
                                 ("orderbook", "orderbook", {"code":ticker})):
        out["requests"] += 1
        try:
            out[key] = fetcher(endpoint, params)
        except Exception as exc:
            # Never include token-bearing HTTP exception URLs in responses/logs.
            out["warnings"].append(f"{endpoint} tidak tersedia ({type(exc).__name__}).")
    cursor, seen_cursors = None, set()
    for _ in range(MAX_PAGES):
        params = {"code": ticker, "minLot": 1}
        if cursor:
            params["cursor"] = cursor
        out["requests"] += 1
        try:
            data = unwrap(fetcher("running-trades", params))
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            out["warnings"].append(f"running-trades tidak tersedia ({type(exc).__name__}, HTTP {status or 'N/A'}).")
            break
        out["pages"] += 1
        out["prints"].extend(rows(data, "items"))
        cursor = data.get("nextCursor")
        out["has_more"] = bool(cursor)
        if not cursor or cursor in seen_cursors or not rows(data, "items"):
            break
        seen_cursors.add(cursor)
    return out


def text_report(m: dict) -> str:
    def price(v):
        return f"{v:,.2f}" if v is not None else "N/A"
    trades = m["trades"]
    span = f"{clock(trades[0]['seconds'])}–{clock(trades[-1]['seconds'])} WIB" if trades else "tidak tersedia"
    lines = [f"📊 TAPE / FOOTPRINT — {m['ticker']}", f"{m['label']} | {m['basis']}",
             f"Diambil: {m['fetched']} (bukan jaminan LIVE)",
             f"Sesi referensi: {m['session'] or 'belum diketahui'} | tanggal tape {'terverifikasi' if m['date_verified'] else 'belum terverifikasi'}",
             f"Harga {price(m['last'])} | Open {price(m['open'])}", "",
             f"HAKA beli agresif: {rp(m['buy'])} ({m['ratio']*100:.1f}%)",
             f"HAKI jual agresif: {rp(m['sell'])} ({(1-m['ratio'])*100:.1f}%)",
             f"Net agresi snapshot: {rp(m['net'], True)}",
             f"VWAP HAKA {price(m['vwap_buy'])} | HAKI {price(m['vwap_sell'])}",
             f"POC nilai sisi terklasifikasi: {price(m['poc'])}",
             f"Antrean snapshot: bid {price(m['best_bid'])} | ask {price(m['best_ask'])}",
             "Antrean bukan transaksi yang sudah match.", "",
             f"Tape: {len(trades)} cetakan | {m['pages']} halaman | {span}",
             f"CVD tape PARSIAL: {rp(m['cvd'][-1], True) if m['cvd'] else 'N/A'}",
             f"Menit net beli {m['minutes_buy']}/{m['minutes_count']} menit yang memiliki transaksi.",
             f"Cakupan {'60 menit terakhir pada tape tersedia' if m['window'] else 'sampel terbaru'}; " + ("masih ada halaman lebih lama." if m['has_more'] else "sesuai respons provider."), "",
             m["reading"], "", "CETAKAN BESAR ≥Rp20 jt (bukan order asli)"]
    lines.extend(f"{clock(r['seconds'])} {'HAKA' if r['action']=='BUY' else 'HAKI'} @ {r['price']:g} | {r['lots']:,.0f} lot | {rp(r['value'])}" for r in m['big'])
    if not m['big']:
        lines.append("Tidak ada dalam sampel.")
    lines += ["", "BATAS DATA", *m["warnings"],
              "DNA/iceberg/dinding/identitas bandar: tidak terverifikasi dari data ini.",
              f"Maks. 7 operasi ZAPI/command; aktual {m['requests']} (retry GET dapat menambah hit). 0 AI. On-demand.",
              "Bantuan: /tape ELTY | /tape ELTY 60 | /help analisis"]
    return "\n".join(lines)


def build_report(ticker: str, *, window=None, fetcher=None) -> TapeReport:
    payloads = fetch_payloads(ticker, fetcher)
    m = analyze(ticker, payloads, window=window)
    report = TapeReport(text_report(m))
    report.metrics = m
    try:
        from orderflow_tape_chart import render_dashboard
        report.dashboard = render_dashboard(m, payloads)
    except Exception as exc:
        print(f"[TAPE-CHART] {type(exc).__name__}")
        report = TapeReport(str(report) + "\nGambar gagal dirender; ringkasan angka tetap tersedia.")
        report.metrics = m
    return report


def send_photo(token: str, chat_id: str, data: bytes, ticker: str) -> dict:
    import requests
    response = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                             data={"chat_id": chat_id, "caption": f"{ticker} • TAPE / FOOTPRINT • lihat cakupan dan batas data pada ringkasan."},
                             files={"photo": (f"tape-{ticker}.png", data, "image/png")}, timeout=45)
    if not response.ok:
        raise RuntimeError(f"Telegram photo failed (HTTP {response.status_code}).")
    result = response.json()
    if result.get("ok") is not True or not result.get("result", {}).get("photo"):
        raise RuntimeError("Telegram tidak mengonfirmasi foto dashboard.")
    print(f"[TAPE-DELIVERY] photo confirmed message_id={result['result']['message_id']}")
    return result
