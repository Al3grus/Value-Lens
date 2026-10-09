"""The website's report: the same content and wording as the terminal views (report/simple.py,
report/detailed.py, report/content.py, report/glossary.py), built as HTML and laid out by the
``.rp`` rules in static/style.css. Borders, score bars and the value gauge are CSS, so nothing
depends on how a font draws box-drawing characters.

Every piece of text goes through ``html.escape``: company names and filing text never become markup.
"""

from __future__ import annotations

from html import escape

from ..analysis.metrics import finite, fmt_money
from ..models import Report, Scorecard, Signal, Status
from ..report import content
from ..report.glossary import HEALTH_MEANING, MEANING, METHOD_MEANING, PILLARS, pillar_word, summarize
from ..report.style import money, pretty_label, score_tone

DOT = "·"
CELLS = 20  # segments in a score bar, as in the terminal view
TONE_CLASS = {"good": "ok", "bad": "bad", "warn": "warn", "muted": "dim", "": ""}
SIGNAL_CLASS = {
    Signal.STRONG_BUY: "strong-buy",
    Signal.BUY: "buy",
    Signal.HOLD: "hold",
    Signal.SELL: "sell",
    Signal.STRONG_SELL: "strong-sell",
}
STATUS_MARK = {
    Status.PASS: ("√", "ok", "passed"),
    Status.FAIL: ("×", "bad", "failed"),
    Status.NA: ("–", "dim", "no data"),
}


def _e(x: object) -> str:
    return escape(str(x), quote=True)


def _t(text: object, tone: str = "", strong: bool = False) -> str:
    """Escaped text, optionally coloured by tone and/or bold."""
    cls = " ".join(c for c in (TONE_CLASS.get(tone, tone), "b" if strong else "") if c)
    return f'<span class="{cls}">{_e(text)}</span>' if cls else _e(text)


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


# ---- building blocks -------------------------------------------------------------------
def _badge(signal: Signal | None) -> str:
    if signal is None:
        return '<span class="rp-badge na">N/A</span>'
    return f'<span class="rp-badge {SIGNAL_CLASS[signal]}">{_e(signal.value)}</span>'


def _meter(score: float | None) -> str:
    filled = 0 if score is None else round(max(0.0, min(100.0, score)) / 100 * CELLS)
    label = "no score" if score is None else f"{score:.0f} out of 100"
    cells = '<i class="on"></i>' * filled + "<i></i>" * (CELLS - filled)
    return f'<span class="rp-meter {TONE_CLASS[score_tone(score)]}" role="img" aria-label="{label}">{cells}</span>'


def _status(status: Status) -> str:
    mark, cls, words = STATUS_MARK[status]
    return f'<td class="st {cls}"><span aria-hidden="true">{mark}</span><span class="sr">{words}</span></td>'


def _bullets(items: list[str], mark: str, tone: str) -> str:
    if not items:
        return ""
    rows = "".join(
        f'<li><span class="mk {TONE_CLASS[tone]}" aria-hidden="true">{mark}</span>{_e(i)}</li>' for i in items
    )
    return f'<ul class="rp-list">{rows}</ul>'


def _kv(rows: list[content.Row]) -> str:
    if not rows:
        return ""
    body = "".join(
        f'<dt>{_e(r.label)}</dt><dd>{_t(r.value, r.tone, r.strong)}</dd><dd class="note">{_e(r.note.strip(f" {DOT}"))}</dd>'
        for r in rows
    )
    return f'<dl class="rp-kv">{body}</dl>'


def _table(head: list[tuple[str, str]], rows: list[str]) -> str:
    """head: (title, css class) per column; rows: ready-made <tr> strings."""
    ths = "".join(f'<th class="{c}">{_e(t)}</th>' if c else f"<th>{_e(t)}</th>" for t, c in head)
    return f'<div class="rp-scroll"><table class="rp-table"><thead><tr>{ths}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _gauge(r: Report) -> str:
    sc = content.scale(r.valuation, r.price)
    if sc is None:
        return ""
    cur = r.currency
    lo, hi = sc.at(sc.low), sc.at(sc.high)
    price = TONE_CLASS[content.price_tone(sc.price, sc.fair)]
    words = (
        f"Value range {money(sc.low, cur, 0)} to {money(sc.high, cur, 0)}, fair value {money(sc.fair, cur, 0)}, "
        f"price {money(sc.price, cur, 0)}"
    )
    return (
        f'<figure class="rp-gauge" role="img" aria-label="{_e(words)}">'
        f'<div class="rp-scale"><span class="rp-end">{_e(money(sc.left, cur, 0))}</span><div class="rp-track">'
        f'<i class="range" style="left:{_pct(lo)};width:{_pct(hi - lo)}"></i>'
        f'<i class="fair" style="left:{_pct(sc.at(sc.fair))}"></i>'
        f'<i class="price {price}" style="left:{_pct(sc.at(sc.price))}"></i>'
        f'</div><span class="rp-end">{_e(money(sc.right, cur, 0))}</span></div>'
        f'<figcaption class="rp-legend" aria-hidden="true">'
        f'<span><i class="key range"></i>value range {_e(money(sc.low, cur, 0))} to {_e(money(sc.high, cur, 0))}</span>'
        f'<span><i class="key fair"></i>fair value {_e(money(sc.fair, cur, 0))}</span>'
        f'<span><i class="key price {price}"></i>price {_e(money(sc.price, cur, 0))}</span>'
        "</figcaption></figure>"
    )


def _panel(title: str, body: str, signal: Signal | None = None, *, flagged: bool = False) -> str:
    sub = f'<div class="rp-sub"><span class="dim">signal</span> {_badge(signal)}</div>' if flagged else ""
    return f'<section class="rp-panel"><h3 class="rp-title">{_e(title)}</h3>{body}{sub}</section>'


def _name_line(r: Report) -> str:
    return f'<span class="rp-co">{_e(r.name)}</span><span class="rp-tk">{_e(r.ticker)}</span>'


# ---- simple view -------------------------------------------------------------------------
def _simple(r: Report) -> str:
    s, d, v = summarize(r), r.decision, r.valuation
    sector = content.meta(r)
    price_line = f'<span class="b">Price {_e(money(r.price, r.currency))}</span>'
    if finite(r.market_cap):
        price_line += (
            f' <span class="dim">{DOT} Market value {_e(fmt_money(r.market_cap, r.currency))}</span>'
        )
    head = (
        f'<header class="rp-head"><h2>{_name_line(r)}</h2>'
        + (f'<p class="dim">{_e(f" {DOT} ".join(sector))}</p>' if sector else "")
        + f"<p>{price_line}</p></header>"
    )
    verdict = (
        f'<section class="rp-sec"><p class="rp-verdict"><span class="b">Verdict</span>{_badge(d.signal)}'
        f'<span class="dim">Confidence: {_e(d.confidence.title())}</span></p>'
        f'<p class="rp-headline {SIGNAL_CLASS[d.signal]}">{_e(s.headline)}</p></section>'
    )

    worth = ['<section class="rp-sec"><h3>What is it worth?</h3>']
    rng = content.value_range(v)
    if rng:
        lo, hi = rng
        price_tone = content.price_tone(r.price, v.fair_value)
        worth.append(
            '<dl class="rp-worth">'
            f'<dt>Fair value</dt><dd><span class="b gold">{_e(money(v.fair_value, r.currency, 0))}</span> per share '
            f'<span class="dim">(range {_e(money(lo, r.currency, 0))} to {_e(money(hi, r.currency, 0))})</span></dd>'
            f"<dt>Price</dt><dd>{_t(money(r.price, r.currency, 0), price_tone, strong=True)} "
            f'<span class="arrow" aria-hidden="true">→</span> {_e(s.value)}</dd></dl>'
        )
        worth.append(_gauge(r))
    else:
        worth.append(f'<p class="dim">{_e(s.value)}</p>')
    if s.expectation:
        worth.append(f'<p class="dim rp-note">{_e(s.expectation)}</p>')
    worth.append("</section>")

    rows = []
    for key, (name, hint) in PILLARS.items():
        score = d.components.get(key)
        rows.append(
            f'<div class="rp-score"><span class="name">{_e(name)}</span>{_meter(score)}'
            f'<span class="num b">{_e(f"{score:.0f}" if score is not None else "n/a")}</span>'
            f'<span class="word b">{_e(pillar_word(key, score))}</span><span class="hint dim">{_e(hint)}</span></div>'
        )
    scores = (
        f'<section class="rp-sec"><h3>Scorecard</h3><div class="rp-scores">{"".join(rows)}</div></section>'
    )

    points = ""
    if s.strengths:
        points += f'<section class="rp-sec"><h3>Strengths</h3>{_bullets(s.strengths, "√", "good")}</section>'
    if s.concerns:
        points += f'<section class="rp-sec"><h3>Concerns</h3>{_bullets(s.concerns, "×", "bad")}</section>'

    timing = (
        f'<section class="rp-sec"><p><span class="b">Timing</span> {_e(d.timing)}</p>'
        f'<p class="dim">Confidence {_e(s.confidence)}</p></section>'
    )
    foot = f'<p class="rp-foot">{_e(content.SIMPLE_FOOTER.format(dot=DOT))}</p>'
    return f'<article class="rp rp-simple">{head}{verdict}{"".join(worth)}{scores}{points}{timing}{foot}</article>'


# ---- detailed view -----------------------------------------------------------------------
def _criteria(card: Scorecard) -> str:
    rows = [
        f"<tr>{_status(c.status)}<td>{_e(pretty_label(c.label, False))}</td>"
        f'<td class="{"" if c.status is not Status.NA else "dim"}">{_e(pretty_label(c.detail, False))}</td>'
        f'<td class="dim">{_e(MEANING.get(c.key, ""))}</td></tr>'
        for c in card.criteria
    ]
    return _table([("", "st"), ("Check", ""), ("Result", ""), ("What it means", "")], rows)


def _score(card: Scorecard, kinds: tuple[str, ...] | None = None) -> str:
    s = content.score_summary(card, kinds)
    if s is None:
        return '<span class="dim">n/a</span>'
    passed, rest, tone = s
    return f'{_t(passed, tone, strong=True)}<span class="dim">{_e(rest)}</span>'


def _mos(mos: float | None) -> str:
    text, tone = content.margin_of_safety(mos)
    return _t(text, tone, strong=tone != "muted")


def _verdict_panel(r: Report) -> str:
    d, s = r.decision, summarize(r)
    sector = content.meta(r)
    head = f'<p class="rp-head-line">{_name_line(r)}' + (
        f'<span class="dim">{_e(f" {DOT} ".join(sector))}</span>' if sector else ""
    )
    head += f'</p><p class="dim">{_e(content.data_line(r, DOT))}</p>'
    verdict = (
        f'<p class="rp-verdict"><span class="b">Verdict</span>{_badge(d.signal)}'
        f'<span class="rp-headline {SIGNAL_CLASS[d.signal]}">{_e(s.headline)}</span></p>'
        f'<p class="dim">Confidence {_e(d.confidence.title())} {DOT} {d.coverage:.0%} of checks had data</p>'
    )
    comp = d.composite
    bars = [
        f'<div class="rp-score total"><span class="name b">Composite</span>{_meter(comp)}'
        f'<span class="num b">{_e(f"{comp:.0f}/100" if comp is not None else "n/a")}</span></div>'
    ]
    for key, (name, hint) in PILLARS.items():
        score = d.components.get(key)
        bars.append(
            f'<div class="rp-score"><span class="name">{_e(name)}</span>{_meter(score)}'
            f'<span class="num">{_e(f"{score:.0f}" if score is not None else "n/a")}</span>'
            f'<span class="hint dim">{_e(f"{pillar_word(key, score)} {DOT} {hint}")}</span></div>'
        )
    none = '<p class="dim">none</p>'
    cols = (
        '<div class="rp-cols">'
        f'<div><h4 class="ok">Strengths</h4>{_bullets(s.strengths, "√", "good") if s.strengths else none}</div>'
        f'<div><h4 class="bad">Concerns</h4>{_bullets(s.concerns, "×", "bad") if s.concerns else none}</div></div>'
    )
    notes = _bullets([pretty_label(x, False) for x in content.verdict_reasons(d)], "•", "warn")
    warnings = _bullets([pretty_label(w, False) for w in d.warnings], "!", "warn") if d.warnings else ""
    timing = f'<p class="rp-gap"><span class="b">Timing</span> {_e(d.timing)}</p>'
    body = (
        f'{head}<div class="rp-gap">{verdict}</div><div class="rp-scores rp-gap">{"".join(bars)}</div>{cols}'
    )
    return _panel("VERDICT", body + f'<div class="rp-gap">{notes}{warnings}</div>' + timing)


def _valuation_panel(r: Report) -> str:
    v = r.valuation
    rows = [
        f'<tr><td class="{"" if m.value else "dim"}">{_e(m.name)}</td>'
        f'<td class="num {"b" if m.value else "dim"}">{_e(money(m.value, r.currency))}</td>'
        f'<td class="num">{m.weight:.0%}</td><td class="dim">{_e(pretty_label(m.note, False))}</td>'
        f'<td class="dim">{_e(METHOD_MEANING.get(m.name, ""))}</td></tr>'
        for m in v.methods
    ]
    parts = [
        _table(
            [
                ("Method", ""),
                ("Per share", "num"),
                ("Weight", "num"),
                ("Assumptions", ""),
                ("What it means", ""),
            ],
            rows,
        )
    ]
    if v.fair_value:
        parts.append(
            f'<p class="rp-gap"><span class="b">Fair value</span> <span class="b gold">{_e(money(v.fair_value, r.currency))}</span>'
            f'<span class="dim rp-sp">DCF bear {_e(money(v.bear, r.currency))} / bull {_e(money(v.bull, r.currency))}</span>'
            f"{_mos(v.margin_of_safety)}</p>"
        )
        parts.append(_gauge(r))
    kv = _kv(content.valuation_rows(r, DOT))
    if kv:
        parts.append(f'<div class="rp-gap">{kv}</div>')
    if v.notes:
        parts.append(_bullets(v.notes, "!", "warn"))
    return _panel("INTRINSIC VALUE", "".join(parts))


def _framework_panel(card: Scorecard, title: str, r: Report, kinds: tuple[str, ...] | None = None) -> str:
    iv, mos = card.extras.get("intrinsic_value"), card.extras.get("margin_of_safety")
    method = card.extras.get("intrinsic_method", "Graham formula")
    foot = (
        f'<p class="rp-facts rp-gap"><span><span class="dim">Score</span> {_score(card, kinds)}</span>'
        f'<span><span class="dim">Value via {_e(method)}</span> <span class="b">{_e(money(iv, r.currency))}</span></span>'
        f"<span>{_mos(mos)}</span></p>"
    )
    return _panel(title, _criteria(card) + foot, card.signal, flagged=True)


def _health_panel(r: Report) -> str:
    rows = []
    for h in r.health:
        status, tone = content.health_status(h)
        rows.append(
            f"<tr>{_status(status)}<td>{_e(h.name)}</td>"
            f'<td class="num">{_e(f"{h.value:.2f}" if finite(h.value) else "n/a")}</td>'
            f'<td class="{TONE_CLASS[tone]}">{_e(h.zone)}</td><td class="dim">{_e(HEALTH_MEANING.get(h.name, ""))}</td></tr>'
        )
    table = _table([("", "st"), ("Score", ""), ("Value", "num"), ("Zone", ""), ("What it means", "")], rows)
    details = "".join(
        f'<p class="dim">{_e(h.name)}: {_e(h.detail)}</p>'
        for h in r.health
        if h.detail and h.value is not None
    )
    return _panel(
        "FINANCIAL HEALTH & FORENSICS", table + (f'<div class="rp-gap">{details}</div>' if details else "")
    )


def _factors_panel(r: Report) -> str:
    score = f'<p class="rp-gap"><span class="dim">Score</span> {_score(r.factors)}</p>'
    return _panel("RESEARCH-BACKED FACTORS", _criteria(r.factors) + score)


def _trend_panel(r: Report) -> str:
    t = r.technicals
    notes = f'<p class="dim rp-gap">Signals: {_e(f" {DOT} ".join(t.notes))}</p>' if t.notes else ""
    return _panel("PRICE TREND (timing only, not scored)", _kv(content.trend_rows(r)) + notes)


def _detailed(r: Report) -> str:
    panels = [
        _verdict_panel(r),
        _valuation_panel(r),
        _framework_panel(r.graham, "GRAHAM: DEFENSIVE INVESTOR", r),
        _framework_panel(r.buffett, "BUFFETT: BUSINESS QUALITY", r, ("quality",)),
        _health_panel(r),
        _factors_panel(r),
        _trend_panel(r),
    ]
    if r.sentiment:
        panels.append(_panel("MARKET SENTIMENT (information only)", _kv(content.sentiment_rows(r, DOT))))
    notes = _bullets([pretty_label(n, False) for n in r.notes], "•", "muted") if r.notes else ""
    stamp = f"{content.DISCLAIMER}  {DOT}  {r.as_of}  {DOT}  {r.elapsed_s:.1f}s"
    panels.append(_panel("NOTES", f'{notes}<p class="dim rp-gap">{_e(stamp)}</p>'))
    return f'<article class="rp rp-detailed">{"".join(panels)}</article>'


def report_html(report: Report, view: str) -> str:
    """HTML for the "simple" or "detailed" view (styled by static/style.css)."""
    return _detailed(report) if view == "detailed" else _simple(report)
