"""Self-contained static HTML report (no server).

Split out of tracker.py: the page was about to grow (score bars, theming, CV
match chips) and render is the only thing run.py:cmd_report needs. tracker.py
imports nothing from here; the dependency runs one way (report -> tracker).
"""

from __future__ import annotations

import html
import shlex

import tracker


def linkedin_links(profile: dict) -> list[tuple[str, str]]:
    links = []
    for kw in profile.get("linkedin_keywords", []):
        q = kw.replace(" ", "%20")
        url = (f"https://www.linkedin.com/jobs/search/?keywords={q}"
               f"&location=Istanbul%2C%20Turkey&f_TPR=r86400")
        links.append((kw, url))
        remote_url = (f"https://www.linkedin.com/jobs/search/?keywords={q}"
                      f"&f_WT=2&f_TPR=r86400")
        links.append((f"{kw} (Remote)", remote_url))
    return links


def kariyer_links(profile: dict) -> list[tuple[str, str]]:
    # kariyer.net's own anti-bot got triggered by two plain page loads during
    # live testing (2026-09-14) - its ToS also prohibits automated collection.
    # Link-only, same treatment as LinkedIn, instead of scraping it.
    links = []
    for kw in profile.get("linkedin_keywords", []):
        q = kw.replace(" ", "+")
        links.append((kw, f"https://www.kariyer.net/is-ilanlari?kw={q}"))
    return links


def render_report(conn, profile: dict, run_id: str, summary: dict, filt=None) -> str:
    rows = tracker.active_jobs(conn)
    esc = html.escape
    # Chips explain the score with the same matcher that produced it. Optional
    # so the selftest's report checks can still call render without a Filter;
    # in that case it builds one from the profile it was already handed.
    filt = filt or tracker.Filter(profile)
    max_score = max((r["score"] for r in rows), default=0) or 1

    def badge(text, cls):
        # cls is built from third-party data (f"wp-{workplace}", f"st-{status}")
        # and lands inside a quoted HTML attribute, so it needs escaping exactly
        # as much as text does. A Workable posting with workplace set to
        # '"><script>...' broke out of the attribute and executed.
        return f'<span class="badge {esc(cls)}">{esc(text)}</span>'

    def safe_url(u):
        # html.escape stops an attacker closing the quote but says nothing about
        # the scheme, and the URL is whatever the board returned. A
        # "javascript:" href runs in a file:// page holding the whole job
        # history. Only http(s) is rendered; anything else becomes a dead link.
        u = (u or "").strip()
        return u if u.lower().startswith(("http://", "https://")) else ""

    sources_seen = sorted({r["source"] for r in rows if r["source"]})
    src_options = "".join(f'<option value="{esc(s)}">{esc(s)}</option>'
                          for s in sources_seen)

    row_html = []
    for r in rows:
        is_new = r["first_seen"] == run_id
        badges = [badge(r["workplace"] or "unknown", f"wp-{r['workplace']}")]
        if is_new:
            badges.append(badge("NEW", "new"))
        if r["status"] != "new":
            badges.append(badge(r["status"], f"st-{r['status']}"))
        skills = filt.matched_skills(r)
        chips = "".join(f'<span class="skill">{esc(s)}</span>' for s in skills)
        pct = min(100, round(100 * r["score"] / max_score))
        # uid is hex today (sha1[:16]); shlex.quote is a no-op then, but keeps the
        # copied command safe to paste if the uid alphabet ever changes.
        cmd = f"uv run python run.py cv build {shlex.quote(r['uid'])}"
        row_html.append(f"""
        <tr class="job-row {'is-new' if is_new else ''}" data-workplace="{esc(r['workplace'] or '')}"
            data-source="{esc(r['source'] or '')}" data-status="{esc(r['status'])}">
          <td class="col-score">
            <div class="score-bar"><span style="width:{pct}%"></span></div>
            <span class="score-num">{r['score']:.0f}</span>
          </td>
          <td class="col-title">
            <a href="{esc(safe_url(r['url']))}" target="_blank" rel="noopener">{esc(r['title'])}</a>
            <div class="badges">{''.join(badges)}</div>
            <div class="skills">{chips}</div>
          </td>
          <td class="col-company">{esc(r['company'])}</td>
          <td class="col-location">{esc(r['location'] or '')}</td>
          <td class="col-posted">{esc(r['posted_at'] or '')}</td>
          <td class="col-source">{esc(r['source'] or '')}</td>
          <td class="col-uid">
            <code>{esc(r['uid'])}</code>
            <button class="copy" data-cmd="{esc(cmd)}" title="copy cv build command">cv&nbsp;build</button>
          </td>
        </tr>""")

    li_rows = "".join(
        f'<a class="li-chip" href="{esc(url)}" target="_blank" rel="noopener">{esc(kw)}</a>'
        for kw, url in linkedin_links(profile)
    )
    kariyer_rows = "".join(
        f'<a class="li-chip" href="{esc(url)}" target="_blank" rel="noopener">{esc(kw)}</a>'
        for kw, url in kariyer_links(profile)
    )

    sources_summary = "".join(
        f"<li><code>{esc(k)}</code>: {v}</li>" for k, v in summary.get("per_source", {}).items()
    )
    # The run already records which sources failed, and nothing rendered it - a
    # run that lost techcareer (146 postings) produced a report byte-identical
    # in shape to a healthy one. A short report is normal; a short report
    # because four boards timed out is not, and only this line says which.
    # A degraded source did not fail - it answered, and under-reported. Same
    # consequence for the reader (missing listings, nothing closed), so it is
    # rendered in the same box rather than a second one nobody would notice.
    errors = (summary.get("errors") or []) + [
        f"{k}: {v}" for k, v in (summary.get("degraded") or {}).items()]
    errors_html = "" if not errors else (
        f'<div class="degraded">⚠ {len(errors)} source(s) failed or under-reported this run — '
        f"listings from them are missing, and none of their jobs were closed."
        f'<ul>{"".join(f"<li><code>{esc(e)}</code></li>" for e in errors)}</ul></div>'
    )

    new_count = sum(1 for r in rows if r["first_seen"] == run_id)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Tracker — {esc(run_id)}</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #f6f7f9; --panel: #ffffff; --fg: #1a1d24; --muted: #667085;
    --border: #e3e6ec; --row-alt: #fbfcfd; --new: #e9f7ec; --new-bar: #2e9e4f;
    --accent: #2f6bff; --chip-bg: #eef2fb; --chip-fg: #2f5fd0; --chip-br: #d3ddf5;
    --skill-bg: #eef6f0; --skill-fg: #1f7a44; --skill-br: #cdebd6;
    --bar-track: #eceef3; --bar-fill: linear-gradient(90deg,#5a86ff,#2f6bff);
    --warn-bg: #fff6e9; --warn-br: #f0c98a; --warn-fg: #8a5a12;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0b0d12; --panel: #12151c; --fg: #e6e8ee; --muted: #8b93a7;
      --border: #262b36; --row-alt: #0f121a; --new: #12200f; --new-bar: #7ee08a;
      --accent: #8ab4ff; --chip-bg: #1a2540; --chip-fg: #8ab4ff; --chip-br: #2c3f6e;
      --skill-bg: #14261b; --skill-fg: #7ee0a0; --skill-br: #234b33;
      --bar-track: #1c2029; --bar-fill: linear-gradient(90deg,#2c3f6e,#5a86ff);
      --warn-bg: #2b1d12; --warn-br: #5c3a1b; --warn-fg: #ffc48a;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0;
          background: var(--bg); color: var(--fg); font-size: 14px; line-height: 1.4; }}
  header {{ padding: 22px 28px 18px; border-bottom: 1px solid var(--border);
            background: var(--panel); }}
  h1 {{ margin: 0 0 4px; font-size: 21px; letter-spacing: -0.01em; }}
  .meta {{ color: var(--muted); font-size: 13px; }}
  .li-row {{ padding: 12px 28px; display: flex; flex-wrap: wrap; gap: 8px;
             border-bottom: 1px solid var(--border); background: var(--panel); }}
  .li-row .lbl {{ color: var(--muted); font-size: 12px; align-self: center;
                  margin-right: 4px; }}
  .li-chip {{ background: var(--chip-bg); color: var(--chip-fg);
              border: 1px solid var(--chip-br); border-radius: 999px;
              padding: 5px 12px; font-size: 12px; text-decoration: none; }}
  .li-chip:hover {{ filter: brightness(1.05); }}
  .controls {{ padding: 12px 28px; display: flex; gap: 10px; flex-wrap: wrap;
               border-bottom: 1px solid var(--border); background: var(--panel);
               position: sticky; top: 0; z-index: 3; }}
  select, input {{ background: var(--bg); color: var(--fg);
                    border: 1px solid var(--border); border-radius: 8px;
                    padding: 7px 11px; font-size: 13px; }}
  input#q {{ flex: 1; min-width: 180px; }}
  .wrap {{ padding: 0 16px 40px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; padding: 11px 12px; color: var(--muted);
        font-size: 12px; font-weight: 600; text-transform: uppercase;
        letter-spacing: 0.03em; border-bottom: 1px solid var(--border);
        cursor: pointer; user-select: none; white-space: nowrap; }}
  td {{ padding: 12px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  tbody tr:nth-child(even) {{ background: var(--row-alt); }}
  tr.is-new td.col-score {{ box-shadow: inset 3px 0 0 var(--new-bar); }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .col-title a {{ font-weight: 600; font-size: 14px; }}
  .col-score {{ width: 92px; }}
  .score-bar {{ height: 6px; border-radius: 999px; background: var(--bar-track);
                overflow: hidden; margin-bottom: 4px; }}
  .score-bar span {{ display: block; height: 100%; background: var(--bar-fill); }}
  .score-num {{ font-variant-numeric: tabular-nums; font-weight: 700;
                font-size: 13px; }}
  .badges {{ margin-top: 5px; }}
  .badge {{ display: inline-block; font-size: 10px; padding: 2px 7px;
            border-radius: 999px; margin: 2px 4px 0 0; background: var(--chip-bg);
            color: var(--chip-fg); }}
  .badge.new {{ background: var(--new); color: var(--new-bar); }}
  .badge.wp-remote {{ background: #1c3a4a22; color: #2b93b8; }}
  .badge.wp-hybrid {{ background: #b8991c22; color: #9a7d17; }}
  .badge.wp-onsite {{ background: #b81c6822; color: #b8437e; }}
  .skills {{ margin-top: 6px; display: flex; flex-wrap: wrap; gap: 4px; }}
  .skill {{ font-size: 10px; padding: 2px 7px; border-radius: 6px;
            background: var(--skill-bg); color: var(--skill-fg);
            border: 1px solid var(--skill-br); }}
  .col-uid code {{ font-size: 10px; color: var(--muted); }}
  .copy {{ display: block; margin-top: 5px; font-size: 10px; cursor: pointer;
           background: var(--chip-bg); color: var(--chip-fg);
           border: 1px solid var(--chip-br); border-radius: 6px; padding: 3px 7px; }}
  .copy:hover {{ filter: brightness(1.05); }}
  footer {{ padding: 20px 28px; color: var(--muted); font-size: 12px; }}
  footer ul {{ columns: 2; }}
  .degraded {{ margin: 14px 16px; padding: 11px 14px; border-radius: 8px;
               background: var(--warn-bg); border: 1px solid var(--warn-br);
               color: var(--warn-fg); font-size: 13px; }}
  .degraded ul {{ margin: 6px 0 0; }}
  @media (max-width: 640px) {{
    .col-company, .col-source {{ display: none; }}
    footer ul {{ columns: 1; }}
  }}
</style></head>
<body>
<header>
  <h1>Job Tracker — Istanbul / Remote DS · ML · AI</h1>
  <div class="meta">Run {esc(run_id)} · {len(rows)} active listings · {new_count} new this run</div>
</header>
<div class="li-row"><span class="lbl">LinkedIn</span>{li_rows}</div>
<div class="li-row"><span class="lbl">kariyer.net</span>{kariyer_rows}</div>
{errors_html}
<div class="controls">
  <input id="q" placeholder="Filter text…" oninput="applyFilters()">
  <select id="wp" onchange="applyFilters()">
    <option value="">All workplaces</option>
    <option value="onsite">On-site</option>
    <option value="hybrid">Hybrid</option>
    <option value="remote">Remote</option>
  </select>
  <select id="status" onchange="applyFilters()">
    <option value="">All statuses</option>
    <option value="new">New</option>
    <option value="interested">Interested</option>
    <option value="applied">Applied</option>
  </select>
  <select id="src" onchange="applyFilters()">
    <option value="">All sources</option>
    {src_options}
  </select>
</div>
<div class="wrap">
<table id="tbl">
  <thead><tr>
    <th onclick="sortBy(0,true)">Score</th>
    <th onclick="sortBy(1)">Title</th>
    <th onclick="sortBy(2)">Company</th>
    <th onclick="sortBy(3)">Location</th>
    <th onclick="sortBy(4)">Posted</th>
    <th onclick="sortBy(5)">Source</th>
    <th>UID</th>
  </tr></thead>
  <tbody>{''.join(row_html)}</tbody>
</table>
</div>
<footer>
  Per-source fetch counts:
  <ul>{sources_summary}</ul>
  Mark status: <code>uv run python run.py mark &lt;uid&gt; applied</code>
</footer>
<script>
function applyFilters() {{
  const q = document.getElementById('q').value.toLowerCase();
  const wp = document.getElementById('wp').value;
  const st = document.getElementById('status').value;
  const src = document.getElementById('src').value;
  document.querySelectorAll('#tbl tbody tr').forEach(tr => {{
    const text = tr.innerText.toLowerCase();
    const okQ = !q || text.includes(q);
    const okWp = !wp || tr.dataset.workplace === wp;
    const okSt = !st || tr.dataset.status === st;
    const okSrc = !src || tr.dataset.source === src;
    tr.style.display = (okQ && okWp && okSt && okSrc) ? '' : 'none';
  }});
}}
function sortBy(idx, numeric) {{
  const tbody = document.querySelector('#tbl tbody');
  const rows = [...tbody.querySelectorAll('tr')];
  const dir = tbody.dataset.sortCol == idx && tbody.dataset.sortDir == 'asc' ? 'desc' : 'asc';
  rows.sort((a, b) => {{
    let x = a.children[idx].innerText.trim(), y = b.children[idx].innerText.trim();
    if (numeric) {{ x = parseFloat(x) || 0; y = parseFloat(y) || 0; return dir === 'asc' ? x - y : y - x; }}
    return dir === 'asc' ? x.localeCompare(y) : y.localeCompare(x);
  }});
  rows.forEach(r => tbody.appendChild(r));
  tbody.dataset.sortCol = idx; tbody.dataset.sortDir = dir;
}}
document.querySelectorAll('.copy').forEach(b => b.onclick = () => {{
  navigator.clipboard.writeText(b.dataset.cmd);
  const t = b.innerHTML; b.textContent = 'copied!';
  setTimeout(() => b.innerHTML = t, 1000);
}});
</script>
</body></html>"""
