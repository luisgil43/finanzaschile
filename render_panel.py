import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def fecha_es(d=None):
    d = d or dt.datetime.now()
    return f"{DIAS[d.weekday()].capitalize()} {d:%d/%m/%Y}"


def fmt_clp(n):
    try:
        return f"$ {format(round(float(n)), ',').replace(',', '.')}"
    except Exception:
        return "N/D"


def fmt_usd(n, dec=0):
    if n is None:
        return "N/D"
    try:
        return f"${round(float(n)):,}" if dec == 0 else f"${float(n):,.{dec}f}"
    except Exception:
        return "N/D"


def fmt_float(n, dec=2):
    if n is None:
        return "N/D"
    try:
        return f"{float(n):.{dec}f}"
    except Exception:
        return "N/D"


def load_font(bold=False, size=36):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def tw(draw, txt, font):
    try:
        return draw.textbbox((0, 0), txt, font=font)[2]
    except Exception:
        return int(len(txt) * (getattr(font, "size", 20) * 0.6))


def _dt_from_latest(data: dict) -> dt.datetime:
    """
    Usa generated_at/fecha desde latest.json para que la fecha del panel
    siempre coincida con el día real del pipeline (y no con UTC).
    """
    tzname = (os.getenv("TZ") or "America/Santiago").strip() or "America/Santiago"
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = None

    gen = (data or {}).get("generated_at")
    if isinstance(gen, str) and gen.strip():
        try:
            d = dt.datetime.fromisoformat(gen.strip())
            if d.tzinfo is None and tz:
                d = d.replace(tzinfo=tz)
            elif d.tzinfo is not None and tz:
                d = d.astimezone(tz)
            return d
        except Exception:
            pass

    f = (data or {}).get("fecha")
    if isinstance(f, str) and f.strip():
        for fmt in ("%d-%m-%Y", "%d/%m/%Y"):
            try:
                d = dt.datetime.strptime(f.strip(), fmt)
                if tz:
                    d = d.replace(tzinfo=tz)
                return d
            except Exception:
                continue

    return dt.datetime.now(tz) if tz else dt.datetime.now()


# -------- Histórico USD/CLP (7 días) ----------
def _mindicador_series():
    """mindicador.cl/api/dolar -> últimos 7 valores (fecha/valor)."""
    try:
        r = requests.get(
            "https://mindicador.cl/api/dolar",
            timeout=10,
            headers={"User-Agent": "finanzas-hoy/1.0"},
        )
        r.raise_for_status()
        j = r.json()
        serie = j.get("serie", [])
        if not serie:
            return []
        puntos = []
        for it in list(serie)[:7][::-1]:
            f = (it.get("fecha") or "")[:10]
            try:
                lab = dt.datetime.fromisoformat(f).strftime("%d/%m")
            except Exception:
                lab = f or ""
            val = it.get("valor")
            if isinstance(val, (int, float)):
                puntos.append((lab, float(val)))
        return puntos
    except Exception:
        return []


def _yahoo_series(ticker: str):
    # Import “perezoso” para bajar peso/errores en Render
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return []

    try:
        df = yf.download(
            ticker, period="12d", interval="1d", progress=False, auto_adjust=True
        )
        if df is None or "Close" not in df or df["Close"].dropna().empty:
            return []
        close = df["Close"].dropna().tail(7)
        return [(idx.strftime("%d/%m"), float(val)) for idx, val in close.items()]
    except Exception:
        return []


def get_usdclp_last_7():
    """Primero mindicador; fallback Yahoo."""
    pts = _mindicador_series()
    if len(pts) >= 2:
        return pts
    for tk in ("USDCLP=X", "CLP=X"):
        pts = _yahoo_series(tk)
        if len(pts) >= 2:
            return pts
    return []


def trend_forecast(values):
    """Pronóstico lineal simple para el siguiente punto."""
    n = len(values)
    if n < 2:
        return None, 0.0
    t = list(range(n))
    st = sum(t)
    sy = sum(values)
    stt = sum(i * i for i in t)
    sty = sum(i * y for i, y in zip(t, values))
    denom = n * stt - st * st
    if denom == 0:
        b = 0.0
        a = values[-1]
    else:
        b = (n * sty - st * sy) / denom
        a = (sy - b * st) / n
    pred = a + b * n
    return pred, b


def draw_sparkline(draw, rect, points, fonts):
    (x1, y1, x2, y2) = rect
    font_lab, font_val = fonts

    if not points or len(points) < 2:
        try:
            draw.rounded_rectangle([x1, y1, x2, y2], radius=18, fill="#08346F")
        except Exception:
            draw.rectangle([x1, y1, x2, y2], fill="#08346F")
        msg = "Sin histórico reciente del dólar."
        draw.text((x1 + 28, (y1 + y2) // 2 - 12), msg, font=font_val, fill="white")
        return

    vals = [v for _, v in points]
    mn, mx = min(vals), max(vals)
    rng = (mx - mn) or 1.0

    pad = 22
    gx1, gy1, gx2, gy2 = x1 + pad, y1 + pad, x2 - pad, y2 - pad
    gw, gh = gx2 - gx1, gy2 - gy1

    try:
        draw.rounded_rectangle([x1, y1, x2, y2], radius=18, fill="#08346F")
    except Exception:
        draw.rectangle([x1, y1, x2, y2], fill="#08346F")

    try:
        draw.line((gx1, gy2, gx2, gy2), fill="#3E68A6", width=1)
    except Exception:
        pass

    n = len(points)
    step_x = gw / (n - 1) if n > 1 else gw
    path = []
    for i, (_, v) in enumerate(points):
        px = gx1 + i * step_x
        py = gy2 - ((v - mn) / rng) * gh
        path.append((px, py))

    for i in range(len(path) - 1):
        try:
            draw.line(
                (path[i][0], path[i][1], path[i + 1][0], path[i + 1][1]),
                fill=(142, 193, 255),
                width=4,
            )
        except Exception:
            pass

    r = 6
    sx, sy = path[0]
    ex, ey = path[-1]
    try:
        draw.ellipse((sx - r, sy - r, sx + r, sy + r), fill="#9FC5FF")
        draw.ellipse((ex - r, ey - r, ex + r, ey + r), fill="#FFFFFF")
    except Exception:
        pass

    last_lbl = f"Último: {vals[-1]:.0f} CLP"
    draw.text((gx1, gy1 - 4), last_lbl, font=font_val, fill="white")

    pred, slope = trend_forecast(vals)
    if pred is not None:
        flecha = "↑" if slope > 0.3 else ("↓" if slope < -0.3 else "→")
        pred_lbl = f"Tendencia: {flecha}  Próx.: {pred:.0f} CLP"
        lw = tw(draw, pred_lbl, font=font_val)
        draw.text((gx2 - lw, gy1 - 4), pred_lbl, font=font_val, fill="#BFD8FF")

    for i, (lab, _) in enumerate(points):
        if i % 2:
            lx = gx1 + i * step_x
            tx = lx - tw(draw, lab, font_lab) // 2
            draw.text((tx, gy2 + 8), lab, font=font_lab, fill="#CFE6FF")


def render(data: dict, outpath: Path):
    W, H = 1920, 1080
    img = Image.new("RGB", (W, H), "#0A1D36")
    d = ImageDraw.Draw(img)

    f_title = load_font(True, 78)
    f_sub = load_font(False, 44)
    f_lab = load_font(False, 44)
    f_val = load_font(True, 58)
    f_wm = load_font(True, 34)

    title = "FINANZAS HOY CHILE"
    d.text(((W - tw(d, title, f_title)) // 2, 56), title, font=f_title, fill="white")

    # ✅ Fecha desde latest.json (generated_at/fecha) con TZ
    sub = fecha_es(_dt_from_latest(data))
    d.text(((W - tw(d, sub, f_sub)) // 2, 56 + 92), sub, font=f_sub, fill="#D0E4FF")

    px1, py1, px2, py2 = 160, 230, W - 160, 800
    try:
        d.rounded_rectangle(
            [px1, py1, px2, py2], radius=26, fill="#0E2C5A", outline="#5CA9FF"
        )
    except Exception:
        d.rectangle([px1, py1, px2, py2], fill="#0E2C5A", outline="#5CA9FF")

    col_gap = 40
    inner_w = px2 - px1 - 80
    col_w = (inner_w - col_gap) // 2
    c1x = px1 + 40
    c2x = px1 + 40 + col_w + col_gap
    row_y = py1 + 36
    step = 72

    def row(label, value, x, y):
        d.text((x, y), label, font=f_lab, fill="#CFE6FF")
        d.text((x + tw(d, label, f_lab) + 18, y - 10), value, font=f_val, fill="white")

    # Izquierda
    row("Dólar (CLP):", fmt_clp(data.get("dolar_clp")), c1x, row_y)
    row("UF:", fmt_clp(data.get("uf_clp")), c1x, row_y + step)
    row("UTM:", fmt_clp(data.get("utm_clp")), c1x, row_y + 2 * step)
    row("Cobre (USD/lb):", fmt_float(data.get("cobre_usd_lb"), 2), c1x, row_y + 3 * step)
    row("Brent (USD/bbl):", fmt_usd(data.get("brent_usd"), 0), c1x, row_y + 4 * step)
    row("Bitcoin (USD):", fmt_usd(data.get("btc_usd"), 0), c1x, row_y + 5 * step)
    row("Ethereum (USD):", fmt_usd(data.get("eth_usd"), 0), c1x, row_y + 6 * step)

    # Derecha (combustibles)
    box_w = col_w
    box_h = 6 * step + 30
    bx1, by1 = c2x, row_y
    bx2, by2 = bx1 + box_w, by1 + box_h
    try:
        d.rounded_rectangle([bx1, by1, bx2, by2], radius=20, fill="#0B2B57", outline="#78B0FF")
    except Exception:
        d.rectangle([bx1, by1, bx2, by2], fill="#0B2B57", outline="#78B0FF")

    d.text((bx1 + 24, by1 + 16), "Combustibles (CLP/L)", font=f_lab, fill="#CFE6FF")
    ry = by1 + 16 + 56
    row("Gasolina 93:", fmt_clp(data.get("g93_clp_l")) + "/L", bx1 + 24, ry)
    ry += step
    row("Gasolina 95:", fmt_clp(data.get("g95_clp_l")) + "/L", bx1 + 24, ry)
    ry += step
    row("Gasolina 97:", fmt_clp(data.get("g97_clp_l")) + "/L", bx1 + 24, ry)
    ry += step
    row("Diésel:", fmt_clp(data.get("diesel_clp_l")) + "/L", bx1 + 24, ry)

    # Sparkline USD/CLP
    SPARK_H = 240
    ny1 = py2 + 24
    ny2 = min(ny1 + SPARK_H, H - 24)
    if ny2 - ny1 < 160:
        ny1 = max(py1 - 16, ny2 - 160)

    try:
        puntos = get_usdclp_last_7()
        draw_sparkline(
            d,
            (px1, ny1, px2, ny2),
            puntos,
            (load_font(False, 26), load_font(True, 30)),
        )
    except Exception:
        try:
            d.rounded_rectangle([px1, ny1, px2, ny2], radius=18, fill="#08346F")
        except Exception:
            d.rectangle([px1, ny1, px2, ny2], fill="#08346F")
        msg = "Sin histórico reciente del dólar."
        d.text((px1 + 28, (ny1 + ny2) // 2 - 12), msg, font=load_font(True, 30), fill="white")

    # Watermark
    wm = "Finanzas Hoy Chile"
    d.text((W - tw(d, wm, f_wm) - 24, 1080 - 24 - 34), wm, font=f_wm, fill="#8EC1FF")

    outpath.parent.mkdir(exist_ok=True, parents=True)
    img.save(outpath, "PNG")


if __name__ == "__main__":
    data = json.loads(Path("data/latest.json").read_text(encoding="utf-8"))
    render(data, Path("out/frame_1080.png"))