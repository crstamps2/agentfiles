"""Visual gate: screenshot every Lookbook scenario of a component and turn the captures into the
PR's Screenshots table. Runs in the runner's own process (no model involved).

Policy (AGENTS.md): QA verification with screenshots happens BEFORE a PR leaves draft. For a
Lookbook-previewed ZUI component the deterministic version of that is: for each scenario the
preview class declares, render `/lookbook/preview/zui/<component>/<scenario>` in headless
Chrome for Testing against the worktree's own dev server, and fail the gate if any scenario
404s, 500s, or renders an exception page.

Chrome for Testing is resolved from Playwright's cache exactly like ~/.claude/bin/
playwright-mcp-chrome does (Browser Testing policy: never system Chrome). Uploads go through
`gh image` (drogers0/gh-image) when installed; otherwise the table lists local paths and the
PR body says so.
"""
from __future__ import annotations

import glob
import os
import pathlib
import re
import subprocess
import urllib.request
import ssl


class VisualGateError(RuntimeError):
    pass


def chrome_binary() -> str:
    cands = glob.glob(os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"))
    if not cands:
        raise VisualGateError("Chrome for Testing not found in Playwright cache (npx playwright install chromium)")
    return sorted(cands, key=os.path.getmtime)[-1]


def worktree_url(wt) -> str:
    """https://admin.<worktree>.test per bin/wt info."""
    name = pathlib.Path(wt).name
    return f"https://admin.{name}.test"


def _get(url: str, timeout=20) -> tuple[int, str]:
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
            return r.status, r.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(20_000).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def ensure_dev_server(wt) -> str:
    """Make sure the worktree's dev server answers and migrations are current."""
    base = worktree_url(wt)
    code, body = _get(f"{base}/lookbook/")
    if code == 500 and "Migrations are pending" in body or code == 0:
        subprocess.run(["bash", "-lc", "bin/wt prepare --for rails"], cwd=str(wt), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        if code == 0:
            subprocess.run(["bash", "-lc", "bin/wt dev --start"], cwd=str(wt), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        code, body = _get(f"{base}/lookbook/")
    if code != 200:
        raise VisualGateError(f"{base}/lookbook/ answered {code}")
    return base


def preview_path(base: str, component: str) -> str:
    """Discover the Lookbook preview path for a component from the Lookbook nav itself. Previews may be
    namespaced (`ZUI::Nav::NavLinkPreview` -> `/lookbook/inspect/zui/nav/nav_link/...`) so the path is not
    simply `zui/<component_dir>` (ZIP-7872 gate false-failed on that assumption, 2026-09-16)."""
    code, body = _get(f"{base}/lookbook/")
    paths = set(re.findall(r'href="/lookbook/inspect/(zui/[a-z0-9_/]+)/[a-z0-9_]+"', body))
    exact = [p for p in paths if p.endswith("/" + component)]
    if exact:
        return sorted(exact, key=len)[0]
    loose = [p for p in paths if p.split("/")[-1].replace("_", "") == component.replace("_", "")]
    if loose:
        return sorted(loose, key=len)[0]
    raise VisualGateError(f"no Lookbook preview found for component {component!r}; nav has {sorted(paths)[:12]}")


def scenarios(base: str, component: str) -> list[str]:
    """Scenario names for the component's preview, from the Lookbook nav."""
    path = preview_path(base, component)
    code, body = _get(f"{base}/lookbook/")
    names = sorted(set(re.findall(rf'href="/lookbook/inspect/{re.escape(path)}/([a-z0-9_]+)"', body)))
    if not names:
        raise VisualGateError(f"no Lookbook scenarios found for {path}")
    return names


def capture(base: str, component: str, out_dir: pathlib.Path, width=1280, height=720) -> dict[str, pathlib.Path]:
    out_dir.mkdir(parents=True, exist_ok=True); chrome = chrome_binary(); shots = {}
    path = preview_path(base, component)
    for name in scenarios(base, component):
        url = f"{base}/lookbook/preview/{path}/{name}"
        code, body = _get(url)
        if code != 200 or "Action Controller: Exception caught" in body:
            raise VisualGateError(f"{url} -> {code}" + (" (exception page)" if "Exception caught" in body else ""))
        png = out_dir / f"{component}-{name}.png"
        r = subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--ignore-certificate-errors",
                            "--virtual-time-budget=8000", f"--window-size={width},{height}", f"--screenshot={png}", url],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if not png.exists() or png.stat().st_size < 1000:
            raise VisualGateError(f"screenshot failed for {url}: {r.stderr.strip()[-300:]}")
        shots[name] = png
    return shots


def upload(paths: list[pathlib.Path], cwd) -> dict[pathlib.Path, str]:
    """`gh image a.png b.png` -> one markdown line per image; returns path -> markdown."""
    if subprocess.run(["gh", "extension", "list"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.find("gh-image") < 0:
        return {}
    r = subprocess.run(["gh", "image", *map(str, paths)], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if r.returncode:
        return {}
    lines = [l for l in r.stdout.splitlines() if l.strip().startswith("![")]
    return dict(zip(paths, lines)) if len(lines) == len(paths) else {}


def screenshots_table(shots: dict[str, pathlib.Path], uploaded: dict[pathlib.Path, str]) -> str:
    rows = ["|Scenario|AFTER|", "|----|----|"]
    for name, p in shots.items():
        cell = uploaded.get(p) or f"`{p}` (local; gh-image upload unavailable)"
        rows.append(f"|`{name}`|{cell}|")
    note = "" if uploaded else "\n\n_Captures exist locally; upload with `gh image` was unavailable when this PR was drafted._"
    return "Headless Chrome for Testing against the worktree's Lookbook, one capture per declared scenario (no BEFORE: net-new component).\n\n" + "\n".join(rows) + note
