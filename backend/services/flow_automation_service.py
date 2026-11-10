import asyncio
import logging
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable

from backend.config import settings
from backend.database.repository import JobRepository

logger = logging.getLogger(__name__)

CHROME_CDP_LAUNCH_INSTRUCTIONS = (
    "Chrome remote debugging is not reachable at {cdp_url}. "
    "To enable hands-free Google Flow automation, start Chrome with:\n"
    '  chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\\ChromeDebug"\n'
    "and sign in to https://flow.google.com (or https://labs.google/fx/tools/flow). "
    "Job has been placed in AWAITING_CLIPS so you can either launch Chrome on port 9222 and click Resume, "
    "or drop clips into your Downloads folder for automatic intake."
)


class CDPConnectionError(RuntimeError):
    """Raised when Playwright cannot connect to Chrome over CDP on port 9222."""
    pass


class FlowAutomationService:
    """
    Chrome DevTools Protocol (CDP) Playwright Automation Service for Google Flow.
    Connects to an authenticated Chrome session on port 9222, locates or opens
    Google Flow, focuses the bottom ProseMirror prompt input alongside the Agent pill,
    submits each scene's 9:16 visual prompt, waits for video rendering, and downloads
    scene_01.mp4 .. scene_04.mp4 into the Downloads folder (and/or job workspace).
    """

    FLOW_URL_PATTERNS = ("flow.google", "labs.google/fx/tools/flow", "labs.google/fx")
    DEFAULT_FLOW_URL = "https://flow.google.com"

    DOWNLOAD_BUTTON_SELECTORS = [
        'button[aria-label*="Download" i]',
        '[role="menuitem"]:has-text("Download")',
        'a[aria-label*="Download" i]',
        'button:has-text("Download")',
        'button:has-text("download")',
        'a[download]',
        '[data-testid*="download" i]',
    ]

    def __init__(
        self,
        cdp_url: Optional[str] = None,
        flow_url: Optional[str] = None,
        scene_timeout_seconds: Optional[int] = None,
        downloads_dir: Optional[Path] = None,
    ):
        self.cdp_url = (cdp_url or settings.CDP_DEBUGGING_URL or "http://127.0.0.1:9222").rstrip("/")
        self.flow_url = flow_url or getattr(settings, "FLOW_URL", self.DEFAULT_FLOW_URL) or self.DEFAULT_FLOW_URL
        self.scene_timeout_seconds = scene_timeout_seconds or getattr(settings, "FLOW_SCENE_TIMEOUT_SECONDS", 180)
        self.downloads_dir = Path(downloads_dir or settings.DOWNLOADS_DIR)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    @classmethod
    def is_cdp_reachable(cls, cdp_url: Optional[str] = None, timeout: float = 2.5) -> bool:
        """Fast pre-check to see if Chrome remote debugging port is listening."""
        target = (cdp_url or settings.CDP_DEBUGGING_URL or "http://127.0.0.1:9222").rstrip("/")
        try:
            req = urllib.request.Request(f"{target}/json/version")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def connect_to_browser(self):
        """
        Connects to Chrome over CDP on http://127.0.0.1:9222.
        Locates an existing tab matching flow.google.com (preferring active /project/ tabs).
        If not open, opens flow.google.com in a new tab.
        """
        if not self.is_cdp_reachable(self.cdp_url):
            msg = CHROME_CDP_LAUNCH_INSTRUCTIONS.format(cdp_url=self.cdp_url)
            logger.warning(msg)
            raise CDPConnectionError(msg)

        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError("Playwright is not installed. Run: pip install playwright") from e

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
        except Exception as e:
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            msg = CHROME_CDP_LAUNCH_INSTRUCTIONS.format(cdp_url=self.cdp_url) + f" (Details: {e})"
            logger.warning(msg)
            raise CDPConnectionError(msg) from e

        contexts = self._browser.contexts
        if not contexts:
            self._context = await self._browser.new_context(accept_downloads=True)
        else:
            self._context = contexts[0]

        # Search all open tabs for an existing Google Flow tab (preferring /project/ canvas tabs)
        matched_page = None
        fallback_flow_page = None
        for ctx in contexts:
            for page in ctx.pages:
                url_lower = (page.url or "").lower()
                if "flow.google.com/project" in url_lower or "labs.google/fx/tools/flow/project" in url_lower:
                    matched_page = page
                    self._context = ctx
                    logger.info(f"[FlowCDP] Found active Google Flow project tab: {page.url}")
                    break
                if fallback_flow_page is None and any(pat in url_lower for pat in self.FLOW_URL_PATTERNS):
                    fallback_flow_page = page
                    self._context = ctx
            if matched_page:
                break

        if matched_page is None and fallback_flow_page is not None:
            matched_page = fallback_flow_page
            logger.info(f"[FlowCDP] Found existing Google Flow tab: {matched_page.url}")

        if matched_page is None:
            logger.info(f"[FlowCDP] No open Google Flow tab found. Opening {self.DEFAULT_FLOW_URL} in a new tab...")
            matched_page = await self._context.new_page()
            try:
                await matched_page.goto(self.DEFAULT_FLOW_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as nav_err:
                logger.warning(f"[FlowCDP] Navigation to {self.DEFAULT_FLOW_URL} reported: {nav_err}")

        self._page = matched_page
        try:
            await self._page.bring_to_front()
            await self._page.wait_for_timeout(400)
        except Exception:
            pass

        return self._page

    async def disconnect(self):
        """Cleanly disconnects the Playwright CDP client without closing the user's Chrome browser."""
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.debug(f"[FlowCDP] Disconnect cleanup note: {e}")
        finally:
            self._playwright = None
            self._browser = None
            self._context = None
            self._page = None

    async def inspect_browser_state(self) -> Dict[str, Any]:
        """
        Connects via CDP, lists all open tabs, and validates DOM accessibility on the active Flow tab.
        """
        page = await self.connect_to_browser()
        tabs_info = []
        for ctx in self._browser.contexts:
            for p in ctx.pages:
                try:
                    t_title = await p.title()
                except Exception:
                    t_title = "<untitled>"
                tabs_info.append({"url": p.url, "title": t_title})

        dom_info = await page.evaluate(
            """() => ({
                readyState: document.readyState,
                title: document.title,
                url: window.location.href,
                contenteditables: document.querySelectorAll('[contenteditable="true"]').length,
                textareas: document.querySelectorAll('textarea').length,
                buttons: document.querySelectorAll('button').length,
                videos: document.querySelectorAll('video').length
            })"""
        )
        return {
            "cdp_url": self.cdp_url,
            "connected": True,
            "open_tabs_count": len(tabs_info),
            "open_tabs": tabs_info,
            "flow_tab": dom_info,
        }

    async def _dismiss_popups_and_reset_focus(self, page):
        """
        Requirement 1: Ensure no modal, flyout menu (such as 'View mode' Grid/Batch settings),
        or open right-hand inspector detail panel blocks the bottom prompt bar.
        Sends Escape and waits 300ms for animations to settle.
        """
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception as e:
            logger.debug(f"[FlowCDP] Escape key press note: {e}")

    async def _close_inspector_panel(self, page):
        """
        Explicitly closes the right-hand inspector detail panel after a scene download
        so that:
        1) The previous scene's inspector download button is no longer visible on-screen.
        2) The bottom prompt input is unobstructed for Scene X+1.
        """
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            # If the inspector download button is still visible, press Escape once more or click close
            dl_btn = await self._find_inspector_download_button(page)
            if dl_btn is not None:
                close_candidates = [
                    page.locator('button[aria-label*="Close" i]').last,
                    page.locator('button:has(mat-icon:has-text("close")), button:has-text("close")').last,
                ]
                closed = False
                for close_btn in close_candidates:
                    try:
                        if await close_btn.count() > 0 and await close_btn.is_visible():
                            await close_btn.click(timeout=1500)
                            closed = True
                            await page.wait_for_timeout(300)
                            break
                    except Exception:
                        continue
                if not closed:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
        except Exception as e:
            logger.debug(f"[FlowCDP] Close inspector panel note: {e}")

    async def _find_prompt_locator(self, page):
        """
        Requirement 2: Locate the prompt input inside the bottom container using
        the multi-fallback selector hierarchy:
        1. page.locator('flow-prompt-box [contenteditable="true"]').last
        2. page.locator('[contenteditable="true"]').last
        3. page.locator('textarea[placeholder*="What do you want to create" i], input[placeholder*="What do you want to create" i]')
        4. page.locator('div:has-text("What do you want to create?")').last
        """
        candidates = [
            page.locator('flow-prompt-box [contenteditable="true"]').last,
            page.locator('[contenteditable="true"]').last,
            page.locator(
                'textarea[placeholder*="What do you want to create" i], '
                'input[placeholder*="What do you want to create" i], '
                'textarea[placeholder*="What would you like to create" i], '
                'input[placeholder*="What would you like to create" i]'
            ).first,
            page.locator('div:has-text("What do you want to create?")').last,
        ]

        # Allow up to 3 seconds for Angular/ProseMirror layout to settle if tab just became active
        for attempt in range(6):
            for loc in candidates:
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        box = await loc.bounding_box()
                        if box and box.get("width", 0) > 20 and box.get("height", 0) > 10:
                            return loc
                except Exception:
                    continue
            if attempt == 2:
                # Dismiss any lingering inspector overlay that might hide the bottom prompt bar
                await self._close_inspector_panel(page)
            await page.wait_for_timeout(300)
        return None

    async def _read_prompt_text(self, prompt_locator) -> str:
        """Reads current text/value from either a contenteditable ProseMirror node or a textarea/input."""
        try:
            text = await prompt_locator.evaluate(
                """(el) => {
                    if (typeof el.value === 'string') return el.value;
                    return el.innerText || el.textContent || '';
                }"""
            )
            cleaned = (text or "").replace("\u200b", "").strip()
            # Ignore placeholder text if the element reports its placeholder in innerText
            if cleaned.lower() in ("what do you want to create?", "what would you like to create?"):
                return ""
            return cleaned
        except Exception:
            return ""

    async def focus_and_fill_prompt(self, page, prompt: str) -> bool:
        """
        Strict Input Field Reset Before Typing Next Scene:
        1. Dismisses any open popup/flyout/inspector panel via Escape + 300ms wait.
        2. Focuses the Flow prompt textarea/ProseMirror element.
        3. Presses Control+A then Backspace.
        4. Checks input.innerText or value to verify it is 100% empty before typing Scene X+1 prompt.
        5. Types the prompt using page.keyboard.type(prompt, delay=15) (with fill fallback).
        """
        await self._dismiss_popups_and_reset_focus(page)

        prompt_locator = await self._find_prompt_locator(page)
        if prompt_locator is None:
            raise RuntimeError(
                f"Could not locate the bottom Google Flow prompt input on {page.url}. "
                "Ensure a Google Flow project canvas is open in Chrome on port 9222."
            )

        # Click explicitly to focus caret inside the editable area
        await prompt_locator.click(timeout=5000)
        await page.wait_for_timeout(150)

        # Clear any previous prompt text safely (Control+A -> Backspace) and verify 100% empty
        for clear_attempt in range(3):
            await page.keyboard.press("Control+A")
            await page.wait_for_timeout(80)
            await page.keyboard.press("Backspace")
            await page.wait_for_timeout(100)

            residual = await self._read_prompt_text(prompt_locator)
            if not residual:
                break

            logger.debug(
                f"[FlowCDP] Prompt input still had {len(residual)} chars after Control+A/Backspace "
                f"(attempt {clear_attempt + 1}); forcing DOM/keyboard clear."
            )
            try:
                await prompt_locator.evaluate(
                    """(el) => {
                        if (typeof el.value === 'string') {
                            el.value = '';
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                        } else if (el.isContentEditable) {
                            el.innerHTML = '';
                            el.dispatchEvent(new InputEvent('input', { bubbles: true }));
                        }
                    }"""
                )
                await page.wait_for_timeout(100)
            except Exception:
                pass

            if not await self._read_prompt_text(prompt_locator):
                break

        final_residual = await self._read_prompt_text(prompt_locator)
        logger.info(f"[FlowCDP] Verified prompt input is 100% empty (residual_len={len(final_residual)}).")

        # Insert the prompt text via keyboard typing
        await prompt_locator.click(timeout=3000)
        await page.keyboard.type(prompt, delay=15)
        await page.wait_for_timeout(200)

        # Verify text was entered; if still empty, fall back to locator.fill(prompt)
        entered_text = await self._read_prompt_text(prompt_locator)
        if not entered_text:
            try:
                await prompt_locator.fill(prompt)
                await page.wait_for_timeout(150)
            except Exception as fill_err:
                logger.debug(f"[FlowCDP] Fallback fill note: {fill_err}")

        return True

    async def _verify_generation_started(self, page, prev_video_count: int) -> bool:
        """
        Verifies after submitting Scene X prompt that the 'Thinking...' / generation loader
        or stop button (`button:has(svg rect)` / `[aria-label*="Stop" i]`) starts.
        """
        for _ in range(10):
            await page.wait_for_timeout(500)
            try:
                started = await page.evaluate(
                    """(prevCount) => {
                        const bodyText = (document.body.innerText || '').toLowerCase();
                        const hasLoaderText = (
                            bodyText.includes('thinking') ||
                            bodyText.includes('generating') ||
                            bodyText.includes('creating') ||
                            bodyText.includes('queued') ||
                            bodyText.includes('%')
                        );
                        const spinners = document.querySelectorAll(
                            '[role="progressbar"], mat-progress-spinner, mat-spinner, ' +
                            '[class*="loading" i], [class*="spinner" i], ' +
                            'button:has(svg rect), [aria-label*="Stop" i], button:has-text("stop")'
                        ).length > 0;
                        const videosNow = document.querySelectorAll('video, flow-video-tile').length;
                        return hasLoaderText || spinners || videosNow > prevCount;
                    }""",
                    prev_video_count,
                )
                if started:
                    logger.info("[FlowCDP] Verified Google Flow 'Thinking...' / stop button / generation loader is active.")
                    return True
            except Exception:
                pass
        logger.debug("[FlowCDP] Generation loader check completed (continuing to video completion poll).")
        return False

    async def _ensure_portrait_aspect_ratio(self, page):
        """
        Requirement 3: Aspect Ratio Check.
        NEVER click top toolbar icons (y < 200), which open the 'View mode' popup.
        Target ONLY the tune/sliders button inside the bottom prompt bar:
          locator('div:has(> [contenteditable], textarea) ~ div button, footer button:has(svg)')
          or the bottom tune button (y > 300).
        If uncertain or already vertical, skip opening the menu and ensure no flyout stays open.
        """
        try:
            bottom_tune_candidates = [
                page.locator('div:has(> [contenteditable], textarea) ~ div button:has-text("tune")').last,
                page.locator('div:has(> [contenteditable], textarea) ~ div button, footer button:has(svg)').first,
                page.locator('button[aria-label="Settings"]:has-text("tune")').last,
            ]

            tune_btn = None
            for cand in bottom_tune_candidates:
                try:
                    if await cand.count() > 0 and await cand.is_visible():
                        box = await cand.bounding_box()
                        # Strictly enforce bottom bar position (y > 300) so top toolbar is NEVER clicked
                        if box and box.get("y", 0) > 300:
                            tune_btn = cand
                            break
                except Exception:
                    continue

            if tune_btn is None:
                logger.debug("[FlowCDP] Bottom tune button not clearly isolated; skipping menu to preserve vertical canvas.")
                return False

            # Check if 9:16 radio option is already visible on-screen without opening menu
            portrait_radio = page.locator('button[role="radio"]:has-text("9:16")').last
            if await portrait_radio.count() > 0 and await portrait_radio.is_visible():
                box = await portrait_radio.bounding_box()
                vp = page.viewport_size or {"width": 1464, "height": 657}
                if box and 0 <= box.get("x", 0) < vp.get("width", 1464):
                    checked = await portrait_radio.get_attribute("aria-checked")
                    if checked != "true":
                        await portrait_radio.click(timeout=2000)
                        logger.info("[FlowCDP] Selected 9:16 Portrait radio option.")
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(200)
                    return True

            logger.debug("[FlowCDP] Skipping tune flyout toggle to avoid obstructing prompt bar.")
            return False
        except Exception as e:
            logger.debug(f"[FlowCDP] Aspect ratio check skipped safely: {e}")
            return False

    async def _submit_prompt(self, page):
        """
        Requirement 4: Locate the submit button inside the bottom prompt container
        (the right-arrow button next to the sliders) or press Enter.
        """
        submit_candidates = [
            page.locator('button[aria-label="Start generation"]').last,
            page.locator('div:has(> [contenteditable], textarea) ~ div button:has-text("arrow_forward")').last,
            page.locator('button:has-text("arrow_forward")').last,
            page.locator('div:has(> [contenteditable], textarea) ~ div button').last,
        ]

        for btn in submit_candidates:
            try:
                if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                    box = await btn.bounding_box()
                    if box and box.get("y", 0) > 300:
                        await btn.click(timeout=3000)
                        logger.info("[FlowCDP] Clicked bottom prompt bar submit button ('Start generation' / arrow_forward).")
                        return
            except Exception:
                continue

        logger.info("[FlowCDP] Bottom submit button not clicked directly; pressing Enter to submit prompt.")
        await page.keyboard.press("Enter")

    async def _snapshot_existing_downloads(self) -> Dict[Path, float]:
        """Records existing video files in Downloads directory before submitting a scene."""
        snapshot: Dict[Path, float] = {}
        try:
            for f in self.downloads_dir.iterdir():
                if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".webm"):
                    snapshot[f.resolve()] = f.stat().st_mtime
        except Exception:
            pass
        return snapshot

    def _find_new_download_in_dir(self, baseline: Dict[Path, float], started_at: float) -> Optional[Path]:
        """Checks if Chrome automatically saved a new .mp4 file into the Downloads folder."""
        try:
            candidates: List[Path] = []
            for f in self.downloads_dir.iterdir():
                if not f.is_file():
                    continue
                if f.name.endswith(".crdownload") or f.name.endswith(".tmp"):
                    continue
                if f.suffix.lower() not in (".mp4", ".mov"):
                    continue
                resolved = f.resolve()
                st = f.stat()
                if st.st_size < 1000:
                    continue
                if resolved not in baseline or st.st_mtime > max(baseline[resolved], started_at - 1.0):
                    candidates.append(resolved)
            if candidates:
                candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return candidates[0]
        except Exception:
            pass
        return None

    async def _snapshot_canvas_cards(self, page) -> Dict[str, Any]:
        """
        Snapshots all COMPLETED video and thumbnail cards on the Google Flow canvas
        (`video`, `flow-video-tile`, `img[src*="/asb/"]`, `div[role="button"]:has(video)`).
        Excludes in-progress placeholder tiles (those containing '%' or 'generating' or lacking `/asb/` media)
        so completion detection only triggers once Google Flow finishes rendering the clip.
        """
        try:
            data = await page.evaluate(
                """() => {
                    const videos = Array.from(document.querySelectorAll('video')).filter(v => {
                        const s = v.currentSrc || v.src || '';
                        return s.includes('/asb/') || s.includes('flow-content.google') || s.startsWith('blob:');
                    });
                    const tiles = Array.from(document.querySelectorAll('flow-video-tile')).filter(t => {
                        const txt = (t.innerText || '').toLowerCase();
                        if (txt.includes('%') || txt.includes('generating') || txt.includes('thinking')) {
                            return false;
                        }
                        const img = t.querySelector('img[src*="/asb/"], img.thumbnail');
                        const vid = t.querySelector('video[src*="/asb/"], video');
                        const hasMedia = Boolean(
                            (img && (img.currentSrc || img.src || '').includes('/asb/')) ||
                            (vid && (vid.currentSrc || vid.src || '').includes('/asb/')) ||
                            txt.includes('play_circle')
                        );
                        return hasMedia;
                    });
                    const thumbImgs = [];
                    for (const t of tiles) {
                        const img = t.querySelector('img');
                        if (img) {
                            const s = img.currentSrc || img.src || '';
                            if (s.includes('/asb/')) thumbImgs.push(s);
                        }
                    }
                    const videoSrcs = videos.map(v => v.currentSrc || v.src || '').filter(Boolean);
                    return {
                        videoCount: videos.length,
                        tileCount: tiles.length,
                        videoSrcs,
                        thumbSrcs: thumbImgs,
                    };
                }"""
            )
            return {
                "video_count": int(data.get("videoCount", 0)),
                "tile_count": int(data.get("tileCount", 0)),
                "video_srcs": set(data.get("videoSrcs", [])),
                "thumb_srcs": set(data.get("thumbSrcs", [])),
            }
        except Exception:
            return {"video_count": 0, "tile_count": 0, "video_srcs": set(), "thumb_srcs": set()}

    async def _download_existing_matching_canvas_card_if_present(
        self,
        page,
        prompt: str,
        target_download_file: Path,
        captured_cdn_urls: List[str],
    ) -> Optional[Path]:
        """
        Checks if a completed `flow-video-tile` on the Google Flow canvas already matches
        the exact prompt for this scene (e.g. from an earlier run on the same project canvas).
        If found, focuses that card, triggers the inspector download + Strategy C CDN stream,
        and returns the saved file path without wasting time re-generating the identical scene.
        """
        try:
            tile_count = await page.locator("flow-video-tile").count()
            if tile_count == 0:
                return None

            prompt_key = prompt.strip()[:48].lower()
            for idx in range(min(tile_count, 6)):
                tile = page.locator("flow-video-tile").nth(idx)
                if not await tile.is_visible():
                    continue
                tile_text = (await tile.inner_text() or "").lower()
                if "%" in tile_text or "generating" in tile_text:
                    continue

                await tile.click(timeout=2500)
                await page.wait_for_timeout(1000)

                inspector_match = await page.evaluate(
                    """(key) => {
                        const vw = window.innerWidth || 1440;
                        const els = Array.from(document.querySelectorAll('div, p, span')).filter(e => {
                            const r = e.getBoundingClientRect();
                            return r.left > vw * 0.50 && r.top > 40 && r.top < 500;
                        });
                        for (const el of els) {
                            const t = (el.innerText || '').trim().toLowerCase();
                            if (t.includes(key)) return true;
                        }
                        return false;
                    }""",
                    prompt_key,
                )
                if inspector_match:
                    dl_btn = await self._find_inspector_download_button(page)
                    if dl_btn is not None:
                        logger.info(
                            f"[FlowCDP] Found existing completed canvas card (Tile {idx}) matching prompt '{prompt_key[:32]}...'. Downloading directly..."
                        )
                        captured_cdn_urls.clear()
                        await dl_btn.click(timeout=3000)
                        await self._click_download_menu_option_if_open(page)
                        await page.wait_for_timeout(1500)

                        arrived = await self._strategy_c_stream_video_to_disk(
                            page, captured_cdn_urls, [], [], target_download_file
                        )
                        await self._close_inspector_panel(page)
                        if arrived and arrived.exists() and arrived.stat().st_size > 100_000:
                            return arrived
                await self._close_inspector_panel(page)
        except Exception as e:
            logger.debug(f"[FlowCDP] Existing canvas card check note: {e}")
        return None

    async def _focus_newest_video_card(
        self,
        page,
        prev_video_srcs: set,
        prev_thumb_srcs: Optional[set] = None,
    ) -> bool:
        """
        Requirement 2: Fix Card Focus & Inspector Download.
        When Scene X generation finishes:
          1. Locate the newest video card on the Flow canvas:
             - Match any newly added `flow-video-tile` / `video` whose thumbnail or video `src` was not in baseline,
             - Or top-most newly added `flow-video-tile` (`page.locator('flow-video-tile').first`),
             - Or `page.locator('video, div[role="button"]:has(video)').last`.
          2. Click that specific card directly to focus it and bring up its dedicated right-hand inspector panel.
          3. Wait 1000ms (`await page.wait_for_timeout(1000)`) for the inspector panel DOM to reflect the new video.
        """
        prev_thumbs = prev_thumb_srcs or set()
        try:
            new_card_handle = await page.evaluate_handle(
                """({prevVidSrcs, prevThumbSrcs}) => {
                    const prevVids = new Set(prevVidSrcs || []);
                    const prevThumbs = new Set(prevThumbSrcs || []);
                    const vw = window.innerWidth || 1440;

                    // 1. Check <flow-video-tile> cards on the canvas (top-most is newest in Google Flow)
                    const tiles = Array.from(document.querySelectorAll('flow-video-tile')).filter(t => {
                        const r = t.getBoundingClientRect();
                        return r.width > 30 && r.height > 30 && r.left < vw * 0.68;
                    });
                    for (const t of tiles) {
                        const img = t.querySelector('img');
                        const vid = t.querySelector('video');
                        const imgSrc = img ? (img.currentSrc || img.src || '') : '';
                        const vidSrc = vid ? (vid.currentSrc || vid.src || '') : '';
                        if ((imgSrc && !prevThumbs.has(imgSrc)) || (vidSrc && !prevVids.has(vidSrc))) {
                            return t;
                        }
                    }

                    // 2. Check standalone <video> or div[role="button"]:has(video) on the canvas
                    const allVideos = Array.from(document.querySelectorAll('video')).filter(v => {
                        const r = v.getBoundingClientRect();
                        return r.width > 20 && r.height > 20 && r.left < vw * 0.68;
                    });
                    for (const v of allVideos) {
                        const src = v.currentSrc || v.src || '';
                        if (src && !prevVids.has(src)) {
                            return v.closest('div[role="button"]') || v.closest('flow-video-tile') || v;
                        }
                    }

                    // 3. Top-most flow-video-tile on canvas (Tile 0 is the most recently generated card)
                    if (tiles.length > 0) {
                        return tiles[0];
                    }
                    if (allVideos.length > 0) {
                        const target = allVideos[allVideos.length - 1];
                        return target.closest('div[role="button"]') || target;
                    }
                    return null;
                }""",
                {"prevVidSrcs": list(prev_video_srcs), "prevThumbSrcs": list(prev_thumbs)},
            )
            elem = new_card_handle.as_element()
            if elem is not None:
                await elem.click(timeout=3000)
                logger.info("[FlowCDP] Clicked newest video card on canvas to focus its right-hand inspector panel.")
                await page.wait_for_timeout(1000)
                return True
        except Exception as e:
            logger.debug(f"[FlowCDP] Primary newest-card focus note: {e}")

        # Fallback to explicit Playwright locators (`flow-video-tile` and `video, div[role="button"]:has(video)`)
        fallback_locators = [
            page.locator("flow-video-tile").first,
            page.locator('video, div[role="button"]:has(video)').last,
        ]
        for card_loc in fallback_locators:
            try:
                if await card_loc.count() > 0 and await card_loc.is_visible():
                    await card_loc.click(timeout=3000)
                    logger.info("[FlowCDP] Clicked fallback video card locator to focus right-hand inspector.")
                    await page.wait_for_timeout(1000)
                    return True
            except Exception as e:
                logger.debug(f"[FlowCDP] Fallback card locator focus note: {e}")

        return False

    async def _find_inspector_download_button(self, page):
        """
        Locates the completed video download button in Google Flow's right-hand detail panel
        using the required selector hierarchy:
        1. page.locator('button[aria-label*="Download" i], button:has(svg path[d*="M19 9h-4V3H9v6H5l7 7 7-7z"])').first
        2. page.locator('.inspector-actions button:first-child').first
        3. page.locator('div:has-text("Omni 1.2")').locator('..').locator('button').first
        4. Any visible button near the top right containing the download tray icon (SVG or mat-icon).
        """
        hierarchy = [
            page.locator('button[aria-label*="Download" i], button:has(svg path[d*="M19 9h-4V3H9v6H5l7 7 7-7z"])').first,
            page.locator('.inspector-actions button:first-child').first,
            page.locator('div:has-text("Omni 1.2")').locator('..').locator('button').first,
            page.locator('button:has(mat-icon:has-text("download")), button:has-text("download")').first,
            page.locator('a[aria-label*="Download" i], a[download]').first,
        ]

        for loc in hierarchy:
            try:
                if await loc.count() > 0 and await loc.is_visible() and await loc.is_enabled():
                    return loc
            except Exception:
                continue

        # Fallback: Any visible button near the top-right inspector panel containing a download tray icon
        try:
            top_right_btn_handle = await page.evaluate_handle(
                """() => {
                const vw = window.innerWidth || 1440;
                const buttons = Array.from(document.querySelectorAll('button, a[role="button"]'));
                for (const b of buttons) {
                    const r = b.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    // Right-hand inspector panel action row (above prompt details, below main header)
                    if (r.left >= vw * 0.50 && r.top >= 15 && r.top <= 420) {
                        const txt = (b.innerText || '').trim().toLowerCase();
                        const aria = (b.getAttribute('aria-label') || b.getAttribute('title') || '').toLowerCase();
                        const paths = Array.from(b.querySelectorAll('svg path')).map(p => p.getAttribute('d') || '').join(' ');
                        if (
                            aria.includes('download') ||
                            txt === 'download' ||
                            txt.includes('file_download') ||
                            paths.includes('M19 9h-4V3H9v6H5l7 7 7-7z') ||
                            paths.includes('M19 9') ||
                            paths.includes('M5 20h14')
                        ) {
                            return b;
                        }
                    }
                }
                return null;
            }"""
            )
            elem = top_right_btn_handle.as_element()
            if elem is not None:
                return elem
        except Exception as e:
            logger.debug(f"[FlowCDP] Top-right download tray scan note: {e}")

        return None

    async def _click_download_menu_option_if_open(self, page) -> bool:
        """
        In Google Flow, clicking `button[aria-label="Download media"]` (`aria-haspopup="menu"`)
        opens an Angular CDK menu (`.cdk-overlay-container`) with resolution options:
          - `720p Original size`
          - `1080p Upscaled`
          - `270p Animated GIF`
        If that menu is open, clicks `720p Original size` (or the first MP4 download option)
        to initiate the actual video download / signed CDN request.
        """
        await page.wait_for_timeout(450)
        menu_candidates = [
            page.locator('.cdk-overlay-container button:has-text("Original size")').first,
            page.locator('.cdk-overlay-container button:has-text("720p")').first,
            page.locator('[role="menuitem"]:has-text("Original size"), [role="menuitem"]:has-text("720p")').first,
            page.locator('.cdk-overlay-container button[mat-menu-item]').nth(1),
        ]
        for item in menu_candidates:
            try:
                if await item.count() > 0 and await item.is_visible():
                    await item.click(timeout=2500)
                    logger.info("[FlowCDP] Clicked '720p Original size' in Google Flow download menu.")
                    return True
            except Exception:
                continue
        return False

    async def _poll_for_arrived_file(
        self,
        page,
        downloads_baseline: Dict[Path, float],
        submit_timestamp: float,
        target_download_file: Path,
        wait_seconds: float = 6.0,
    ) -> Optional[Path]:
        """
        Polls `Downloads/` and `incoming_clips/` for up to `wait_seconds` (default 6s)
        without blocking on `page.expect_download()`.
        """
        steps = max(1, int(wait_seconds / 0.5))
        for _ in range(steps):
            await page.wait_for_timeout(500)
            if target_download_file.exists() and target_download_file.stat().st_size > 1000:
                if target_download_file.stat().st_mtime >= submit_timestamp - 1.0:
                    return target_download_file

            new_dl = self._find_new_download_in_dir(downloads_baseline, submit_timestamp)
            if new_dl and new_dl.exists() and new_dl.stat().st_size > 1000:
                if new_dl.resolve() != target_download_file.resolve():
                    shutil.move(str(new_dl), str(target_download_file))
                return target_download_file
        return None

    async def _strategy_b_hover_canvas_card_download(
        self,
        page,
        downloads_baseline: Dict[Path, float],
        submit_timestamp: float,
        target_download_file: Path,
    ) -> Optional[Path]:
        """
        Strategy B (Fallback): Hover over the canvas video card directly and click the floating
        card overlay download button (or More options -> Download).
        """
        try:
            card_loc = page.locator("flow-video-tile, video, div[role='button']:has(video)").first
            if await card_loc.count() == 0 or not await card_loc.is_visible():
                return None

            await card_loc.hover(timeout=2500)
            await page.wait_for_timeout(450)

            overlay_dl_candidates = [
                card_loc.locator('button[aria-label*="Download" i], button:has-text("download")').first,
                page.locator('flow-video-edit-history-hotbar button[aria-label*="Download" i]').first,
                card_loc.locator('button[aria-label*="More" i], button:has-text("more_vert")').first,
            ]
            for btn in overlay_dl_candidates:
                try:
                    if await btn.count() > 0 and await btn.is_visible():
                        aria = (await btn.get_attribute("aria-label") or "").lower()
                        await btn.click(timeout=2500)
                        logger.info(f"[FlowCDP] Strategy B: Clicked floating card overlay button (aria='{aria}').")
                        await self._click_download_menu_option_if_open(page)
                        arrived = await self._poll_for_arrived_file(
                            page, downloads_baseline, submit_timestamp, target_download_file, wait_seconds=4.0
                        )
                        if arrived:
                            return arrived
                        break
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"[FlowCDP] Strategy B card hover fallback note: {e}")
        return None

    async def _strategy_c_stream_video_to_disk(
        self,
        page,
        captured_cdn_urls: List[str],
        new_srcs: List[str],
        curr_video_srcs: List[str],
        target_download_file: Path,
    ) -> Optional[Path]:
        """
        Strategy C (Direct Blob / Signed CDN URL Extraction Fallback):
        Reads the captured `https://flow-content.google/video/...` signed URL, active `<video src>`,
        or `blob:` URL and streams the MP4 bytes directly to `target_download_file`.
        """
        candidate_urls: List[str] = []
        for u in reversed(captured_cdn_urls):
            if u and u not in candidate_urls:
                candidate_urls.append(u)
        for u in new_srcs + curr_video_srcs:
            if u and u not in candidate_urls:
                candidate_urls.append(u)

        # Also query any live <video> or <source> or <a href*="flow-content.google"> in DOM
        try:
            dom_urls = await page.evaluate(
                """() => {
                    const out = [];
                    for (const v of document.querySelectorAll('video, source')) {
                        const s = v.currentSrc || v.src || '';
                        if (s) out.push(s);
                    }
                    for (const a of document.querySelectorAll('a[href]')) {
                        const h = a.href || '';
                        if (h.includes('flow-content.google/video') || h.includes('.mp4')) out.push(h);
                    }
                    return out;
                }"""
            )
            for u in dom_urls or []:
                if u and u not in candidate_urls:
                    candidate_urls.append(u)
        except Exception:
            pass

        for target_src in candidate_urls:
            try:
                if target_src.startswith("http"):
                    logger.info(f"[FlowCDP] Strategy C: Streaming video directly from URL -> {target_src[:85]}...")
                    data = b""
                    try:
                        resp = await page.context.request.get(target_src, timeout=45000)
                        if resp.status == 200:
                            data = await resp.body()
                    except Exception as ctx_err:
                        logger.debug(f"[FlowCDP] Strategy C context.request note: {ctx_err}")

                    if len(data) < 100_000 or b"ftyp" not in data[:32]:
                        req = urllib.request.Request(target_src, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=45) as u_resp:
                            data = u_resp.read()

                    if len(data) > 100_000 and b"ftyp" in data[:32]:
                        target_download_file.write_bytes(data)
                        logger.info(
                            f"[FlowCDP] Strategy C succeeded: wrote {len(data):,} valid MP4 bytes to {target_download_file}"
                        )
                        return target_download_file
                    else:
                        logger.debug(
                            f"[FlowCDP] Strategy C skipped non-MP4 payload ({len(data)} bytes) from {target_src[:60]}"
                        )
                elif target_src.startswith("blob:"):
                    logger.info(f"[FlowCDP] Strategy C: Extracting blob URL via browser fetch -> {target_src[:60]}...")
                    b64_data = await page.evaluate(
                        """async (blobUrl) => {
                            const res = await fetch(blobUrl);
                            const buf = await res.arrayBuffer();
                            const bytes = new Uint8Array(buf);
                            let binary = '';
                            const chunk = 0x8000;
                            for (let i = 0; i < bytes.length; i += chunk) {
                                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                            }
                            return btoa(binary);
                        }""",
                        target_src,
                    )
                    if b64_data:
                        import base64
                        raw_bytes = base64.b64decode(b64_data)
                        if len(raw_bytes) > 100_000 and b"ftyp" in raw_bytes[:32]:
                            target_download_file.write_bytes(raw_bytes)
                            logger.info(
                                f"[FlowCDP] Strategy C (blob) succeeded: wrote {len(raw_bytes):,} bytes to {target_download_file}"
                            )
                            return target_download_file
            except Exception as src_err:
                logger.debug(f"[FlowCDP] Strategy C stream attempt note for {target_src[:60]}: {src_err}")

        return None

    @staticmethod
    def _is_duplicate_of_other_scene(path: Path, job_id: Optional[str], scene_num: int) -> bool:
        """True if `path` is byte-identical to another scene clip already saved for this job."""
        import hashlib
        if not job_id:
            return False
        clips_dir = settings.JOBS_STORAGE_DIR / job_id / "incoming_clips"
        try:
            h = hashlib.sha256(path.read_bytes()).hexdigest()
            for other in clips_dir.glob("scene_*.mp4"):
                if other.resolve() == path.resolve():
                    continue
                if hashlib.sha256(other.read_bytes()).hexdigest() == h:
                    return True
        except Exception:
            pass
        return False

    async def generate_and_download_scene(
        self,
        prompt: str,
        scene_index: int,
        output_path: Optional[Path] = None,
        job_id: Optional[str] = None,
    ) -> Path:
        """
        Automates generating and downloading a single scene in Google Flow with Multi-Strategy Resiliency:
        1. Input Reset & Sequence Guard:
           - Dismisses popups/inspector via Escape.
           - Selects all and backspaces (`Control+A` -> `Backspace`), verifying `innerText`/`value` is 100% empty.
           - Types Scene X prompt and submits.
           - Verifies generation starts (`Thinking...`, progress spinner, or stop button `button:has(svg rect)`).
        2. Resilient Completion Polling:
           - Checks both `<video>` creation and `<flow-video-tile>` thumbnail status.
        3. Multi-Strategy Download Dispatch (never hangs on `page.expect_download()`):
           - Focuses the newest canvas card (`flow-video-tile` / `video`) and waits 1000ms.
           - Strategy A: Clicks inspector download button (`button[aria-label*="Download" i]`,
             `button:has(svg path[d*="M19 9h-4V3H9v6H5l7 7 7-7z"])`, `.inspector-actions button:first-child`)
             and selects `720p Original size` if the resolution menu opens; waits up to 6s for file arrival.
           - Strategy B (Fallback): If Strategy A does not yield a file within 6s, hovers over the canvas card
             and clicks the floating card overlay download action.
           - Strategy C (Direct CDN / Blob Stream Fallback): Streams the captured `flow-content.google/video/...`
             signed URL, `<video src>`, or blob URL directly to disk.
        4. Post-download cleanup: Closes inspector panel immediately using `Escape` or close icon.
        """
        if self._page is None:
            await self.connect_to_browser()

        page = self._page
        scene_num = scene_index + 1

        # Determine direct incoming_clips/scene_0X.mp4 path if job_id is present
        incoming_clip_path: Optional[Path] = None
        if job_id:
            incoming_dir = settings.JOBS_STORAGE_DIR / job_id / "incoming_clips"
            incoming_dir.mkdir(parents=True, exist_ok=True)
            incoming_clip_path = incoming_dir / f"scene_{scene_num:02d}.mp4"

        target_download_file = incoming_clip_path or output_path or (self.downloads_dir / f"scene_{scene_num:02d}.mp4")
        target_download_file.parent.mkdir(parents=True, exist_ok=True)

        if job_id:
            JobRepository.add_log(
                job_id,
                "INFO",
                "FLOW_AUTOMATION",
                f"GENERATING_FLOW_VISUALS (Scene {scene_num}/4): Submitting prompt to Google Flow..."
            )

        # Ensure any previous scene's right-hand inspector detail panel is closed BEFORE snapshotting
        await self._close_inspector_panel(page)

        # Snapshot current canvas cards (<video>, <flow-video-tile>, thumbnails) and Downloads folder state
        baseline_cards = await self._snapshot_canvas_cards(page)
        prev_video_count = baseline_cards["video_count"]
        prev_tile_count = baseline_cards["tile_count"]
        prev_video_srcs = baseline_cards["video_srcs"]
        prev_thumb_srcs = baseline_cards["thumb_srcs"]
        downloads_baseline = await self._snapshot_existing_downloads()
        submit_timestamp = time.time()

        # Listen for signed Google Flow CDN video URLs (`https://flow-content.google/video/...`) for Strategy C
        captured_cdn_urls: List[str] = []

        def _on_request(req):
            try:
                u = req.url or ""
                if "flow-content.google/video/" in u and u not in captured_cdn_urls:
                    captured_cdn_urls.append(u)
            except Exception:
                pass

        page.on("request", _on_request)

        try:
            # 0. Check if a completed card on the Google Flow canvas already matches this scene's exact prompt
            existing_card_dl = await self._download_existing_matching_canvas_card_if_present(
                page, prompt, target_download_file, captured_cdn_urls
            )
            if existing_card_dl and existing_card_dl.exists() and existing_card_dl.stat().st_size > 100_000:
                downloaded_path = existing_card_dl
            else:
                # Re-snapshot after closing inspector
                baseline_cards = await self._snapshot_canvas_cards(page)
                prev_video_count = baseline_cards["video_count"]
                prev_tile_count = baseline_cards["tile_count"]
                prev_video_srcs = baseline_cards["video_srcs"]
                prev_thumb_srcs = baseline_cards["thumb_srcs"]

                # Requirement 1: Dismiss popups, focus bottom prompt input, strict clear & empty-check, type prompt
                await self.focus_and_fill_prompt(page, prompt)

                # Aspect ratio check (bottom bar only, never top toolbar)
                await self._ensure_portrait_aspect_ratio(page)

                # Submit prompt & verify "Thinking..." / stop button / generation loader starts
                await self._submit_prompt(page)
                await self._verify_generation_started(page, prev_video_count + prev_tile_count)
                logger.info(
                    f"[FlowCDP] Scene {scene_num} prompt submitted. "
                    f"Waiting up to {self.scene_timeout_seconds}s for Flow generation & multi-strategy download..."
                )

                start_ts = time.time()
                downloaded_path = None
                clicked_video_to_open_inspector = False
                download_triggered = False

                while time.time() - start_ts < self.scene_timeout_seconds:
                    await page.wait_for_timeout(2500)

                    # A. Check if a new file already landed in Downloads directory or incoming_clips/
                    new_dl = self._find_new_download_in_dir(downloads_baseline, submit_timestamp)
                    if new_dl and new_dl.exists() and new_dl.stat().st_size > 1000:
                        if new_dl.resolve() != target_download_file.resolve():
                            shutil.move(str(new_dl), str(target_download_file))
                        downloaded_path = target_download_file
                        logger.info(f"[FlowCDP] Moved new download from Downloads folder -> {target_download_file}")
                        break

                    # B. Check if generation has completed (new <video>, new <flow-video-tile>, or new thumbnail src)
                    curr_cards = await self._snapshot_canvas_cards(page)
                    curr_video_count = curr_cards["video_count"]
                    curr_tile_count = curr_cards["tile_count"]
                    curr_video_srcs = list(curr_cards["video_srcs"])
                    new_srcs = [s for s in curr_video_srcs if s not in prev_video_srcs]
                    new_thumbs = [t for t in curr_cards["thumb_srcs"] if t not in prev_thumb_srcs]

                    video_finished = (
                        curr_video_count > prev_video_count
                        or curr_tile_count > prev_tile_count
                        or len(new_srcs) > 0
                        or len(new_thumbs) > 0
                    )

                    if not video_finished:
                        continue

                    # Prompt-verified download: only accept the card whose inspector prompt matches THIS scene
                    await page.wait_for_timeout(2000)
                    captured_cdn_urls.clear()
                    matched = await self._download_existing_matching_canvas_card_if_present(
                        page, prompt, target_download_file, captured_cdn_urls
                    )
                    if matched and matched.exists() and matched.stat().st_size > 100_000:
                        if self._is_duplicate_of_other_scene(matched, job_id, scene_num):
                            logger.warning(f"[FlowCDP] Scene {scene_num} clip duplicates another scene; retrying.")
                            matched.unlink(missing_ok=True)
                            continue
                        downloaded_path = matched
                        logger.info(f"[FlowCDP] Prompt-verified download succeeded for Scene {scene_num}.")
                        break
                    # Card not yet matching/ready; keep polling instead of falling back to a possibly stale card
                    continue

                    # (legacy fallback below is intentionally unreachable)
                    if not clicked_video_to_open_inspector:
                        clicked_video_to_open_inspector = await self._focus_newest_video_card(
                            page, prev_video_srcs, prev_thumb_srcs=prev_thumb_srcs
                        )

                    dl_btn = await self._find_inspector_download_button(page)
                    if dl_btn is None:
                        await self._focus_newest_video_card(page, prev_video_srcs, prev_thumb_srcs=prev_thumb_srcs)
                        dl_btn = await self._find_inspector_download_button(page)

                    # Multi-Strategy Download Dispatch (runs once per completed scene)
                    if not download_triggered:
                        download_triggered = True

                        # --- STRATEGY A: Inspector Download Button + Resolution Menu + 6s Arrival Check ---
                        if dl_btn is not None:
                            logger.info(
                                f"[FlowCDP] Strategy A: Clicking inspector download button for Scene {scene_num}..."
                            )
                            try:
                                captured_cdn_urls.clear()
                                await dl_btn.click(timeout=4000)
                                await self._click_download_menu_option_if_open(page)
                                arrived = await self._poll_for_arrived_file(
                                    page,
                                    downloads_baseline,
                                    submit_timestamp,
                                    target_download_file,
                                    wait_seconds=6.0,
                                )
                                if arrived and arrived.exists() and arrived.stat().st_size > 1000:
                                    downloaded_path = arrived
                                    logger.info(
                                        f"[FlowCDP] Strategy A succeeded for Scene {scene_num} -> {downloaded_path}"
                                    )
                                    break
                            except Exception as strat_a_err:
                                logger.debug(f"[FlowCDP] Strategy A note: {strat_a_err}")

                        # --- STRATEGY B: Hover Canvas Card & Click Floating Overlay Download Button ---
                        if not downloaded_path:
                            logger.info(
                                f"[FlowCDP] Strategy A did not produce a local file within 6s "
                                f"(captured_cdn_urls={len(captured_cdn_urls)}). Trying Strategy B (canvas card hover)..."
                            )
                            arrived_b = await self._strategy_b_hover_canvas_card_download(
                                page, downloads_baseline, submit_timestamp, target_download_file
                            )
                            if arrived_b and arrived_b.exists() and arrived_b.stat().st_size > 1000:
                                downloaded_path = arrived_b
                                logger.info(
                                    f"[FlowCDP] Strategy B succeeded for Scene {scene_num} -> {downloaded_path}"
                                )
                                break

                        # --- STRATEGY C: Direct Signed CDN / <video src> / Blob URL Stream to Disk ---
                        if not downloaded_path:
                            logger.info(
                                f"[FlowCDP] Trying Strategy C (Direct CDN / Blob URL stream) for Scene {scene_num}..."
                            )
                            arrived_c = await self._strategy_c_stream_video_to_disk(
                                page, captured_cdn_urls, new_srcs, curr_video_srcs, target_download_file
                            )
                            if arrived_c and arrived_c.exists() and arrived_c.stat().st_size > 1000:
                                downloaded_path = arrived_c
                                logger.info(
                                    f"[FlowCDP] Strategy C succeeded for Scene {scene_num} -> {downloaded_path}"
                                )
                                break

                        # If none of A/B/C succeeded on first pass, allow one retry on the next poll iteration
                        download_triggered = False

            if not downloaded_path or not downloaded_path.exists() or downloaded_path.stat().st_size < 1000:
                raise TimeoutError(
                    f"Google Flow scene {scene_num} generation timed out after {self.scene_timeout_seconds}s on {page.url}."
                )

            # Clean up any duplicate copy Chrome may have simultaneously dropped into Downloads/
            extra_dl = self._find_new_download_in_dir(downloads_baseline, submit_timestamp)
            if extra_dl and extra_dl.exists() and extra_dl.resolve() != downloaded_path.resolve():
                try:
                    extra_dl.unlink()
                    logger.info(f"[FlowCDP] Removed duplicate raw browser download '{extra_dl.name}' from Downloads/.")
                except OSError:
                    pass

            # Register this direct scene download with DownloadsWatcher so any delayed duplicate is deduplicated
            try:
                from backend.services.downloads_watcher import DownloadsWatcher
                DownloadsWatcher.get_instance().register_direct_scene_download(
                    job_id=job_id,
                    scene_index=scene_index,
                    file_size=downloaded_path.stat().st_size,
                    target_path=downloaded_path,
                )
            except Exception as reg_err:
                logger.debug(f"[FlowCDP] DownloadsWatcher registration note: {reg_err}")

            # Ensure both incoming_clips/scene_0X.mp4 and output_path (scenes/scene_0X.mp4) have the clip
            if incoming_clip_path and incoming_clip_path.resolve() != downloaded_path.resolve():
                incoming_clip_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(downloaded_path), str(incoming_clip_path))

            if output_path and output_path.resolve() != downloaded_path.resolve():
                output_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(downloaded_path), str(output_path))

            # Post-download cleanup: Close inspector panel immediately using Escape or close icon
            await page.wait_for_timeout(1500)
            await self._close_inspector_panel(page)

            return output_path if (output_path and output_path.exists()) else downloaded_path
        finally:
            try:
                page.remove_listener("request", _on_request)
            except Exception:
                pass

    async def generate_all_scenes(
        self,
        prompts: List[str],
        job_id: Optional[str] = None,
        output_dir: Optional[Path] = None,
        on_scene_start: Optional[Callable[[int, int], None]] = None,
    ) -> List[Path]:
        """Runs through all scenes sequentially with safety timeouts and error handling."""
        results: List[Path] = []
        total = len(prompts)
        try:
            await self.connect_to_browser()
            for idx, prompt in enumerate(prompts):
                if on_scene_start:
                    on_scene_start(idx + 1, total)
                out_file = (output_dir / f"scene_{idx + 1:02d}.mp4") if output_dir else None
                clip_path = await self.generate_and_download_scene(
                    prompt=prompt,
                    scene_index=idx,
                    output_path=out_file,
                    job_id=job_id,
                )
                results.append(clip_path)
            return results
        finally:
            await self.disconnect()

    def run_scene_sync(
        self,
        prompt: str,
        scene_index: int,
        output_path: Optional[Path] = None,
        job_id: Optional[str] = None,
    ) -> Path:
        """Synchronous wrapper for generate_and_download_scene."""
        async def _runner():
            try:
                await self.connect_to_browser()
                return await self.generate_and_download_scene(
                    prompt=prompt,
                    scene_index=scene_index,
                    output_path=output_path,
                    job_id=job_id,
                )
            finally:
                await self.disconnect()

        return self._run_coro_sync(_runner())

    @staticmethod
    def _run_coro_sync(coro):
        """Executes an async coroutine synchronously even if an event loop is running in another thread."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: asyncio.run(coro)).result()
        return asyncio.run(coro)
