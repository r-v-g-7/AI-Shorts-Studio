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
        Requirement 1: Ensure no modal or flyout menu (such as 'View mode' Grid/Batch settings)
        is active at the start of a scene interaction.
        Sends Escape and waits 300ms for animations to settle.
        """
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception as e:
            logger.debug(f"[FlowCDP] Escape key press note: {e}")

    async def _find_prompt_locator(self, page):
        """
        Requirement 2: Locate the prompt input inside the bottom container using
        the multi-fallback selector hierarchy:
        1. page.locator('[contenteditable="true"]').last
        2. page.locator('textarea[placeholder*="What do you want to create" i], input[placeholder*="What do you want to create" i]')
        3. page.locator('div:has-text("What do you want to create?")').last
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
        for _ in range(6):
            for loc in candidates:
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        box = await loc.bounding_box()
                        if box and box.get("width", 0) > 20 and box.get("height", 0) > 10:
                            return loc
                except Exception:
                    continue
            await page.wait_for_timeout(300)
        return None

    async def focus_and_fill_prompt(self, page, prompt: str) -> bool:
        """
        Requirement 1 & 2:
        - Dismisses any open popup/flyout via Escape + 300ms wait.
        - Targets the bottom prompt input.
        - Clicks explicitly to focus the caret.
        - Clears previous text safely (Control+A -> Backspace).
        - Types the prompt using page.keyboard.type(prompt, delay=15) (with fill fallback)
          so text is reliably inserted alongside any 'Agent' pill badge.
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

        # Clear any previous prompt text safely without removing external sibling pills
        await page.keyboard.press("Control+A")
        await page.wait_for_timeout(80)
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(100)

        # Insert the prompt text via keyboard typing
        await page.keyboard.type(prompt, delay=15)
        await page.wait_for_timeout(200)

        # Verify text was entered; if still empty, fall back to locator.fill(prompt)
        entered_text = ""
        try:
            entered_text = (await prompt_locator.inner_text() or "").strip()
        except Exception:
            pass

        if not entered_text:
            try:
                await prompt_locator.fill(prompt)
                await page.wait_for_timeout(150)
            except Exception as fill_err:
                logger.debug(f"[FlowCDP] Fallback fill note: {fill_err}")

        return True

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

    async def _find_inspector_download_button(self, page):
        """
        Locates the completed video download button in Google Flow's right-hand detail panel
        using the required selector hierarchy:
        1. page.locator('button:has(svg path[d*="M19 9h-4V3H9v6H5l7 7 7-7z"]), button[aria-label*="Download" i]').first
        2. page.locator('div:has-text("Omni 1.2")').locator('..').locator('button').first
        3. Any visible button near the top right containing the download tray icon (SVG or mat-icon).
        """
        hierarchy = [
            page.locator('button:has(svg path[d*="M19 9h-4V3H9v6H5l7 7 7-7z"]), button[aria-label*="Download" i]').first,
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

        # Fallback 3: Any visible button near the top-right inspector panel containing a download tray icon
        try:
            top_right_btn_handle = await page.evaluate_handle(
                """() => {
                const vw = window.innerWidth || 1440;
                const buttons = Array.from(document.querySelectorAll('button, a[role="button"]'));
                for (const b of buttons) {
                    const r = b.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    // Right-hand inspector panel action row (above prompt details, below main header)
                    if (r.left >= vw * 0.50 && r.top >= 45 && r.top <= 420) {
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

    async def generate_and_download_scene(
        self,
        prompt: str,
        scene_index: int,
        output_path: Optional[Path] = None,
        job_id: Optional[str] = None,
    ) -> Path:
        """
        Automates generating and downloading a single scene in Google Flow:
        1. Dismisses any open popup/flyout ('View mode') via Escape + 300ms settle.
        2. Focuses the bottom prompt box ('What do you want to create?'), clears via
           Control+A + Backspace, and types the visual prompt via keyboard.type(delay=15).
        3. Checks aspect ratio safely (never clicking top toolbar icons).
        4. Submits the prompt via the bottom-bar right-arrow button or Enter.
        5. Waits up to 180s for video generation completion and clicks the right-hand
           detail panel download button as soon as generation finishes.
        """
        if self._page is None:
            await self.connect_to_browser()

        page = self._page
        scene_num = scene_index + 1
        target_download_file = self.downloads_dir / f"scene_{scene_num:02d}.mp4"

        if job_id:
            JobRepository.add_log(
                job_id,
                "INFO",
                "FLOW_AUTOMATION",
                f"GENERATING_FLOW_VISUALS (Scene {scene_num}/4): Submitting prompt to Google Flow..."
            )

        # Snapshot current video elements and Downloads folder state before submitting
        prev_video_count = await page.locator("video").count()
        prev_video_srcs = set(
            await page.evaluate(
                """() => Array.from(document.querySelectorAll('video'))
                    .map(v => v.currentSrc || v.src || '')
                    .filter(Boolean)"""
            )
        )
        downloads_baseline = await self._snapshot_existing_downloads()
        submit_timestamp = time.time()

        # Requirement 1 & 2: Dismiss popups, focus bottom prompt input, clear & type prompt
        await self.focus_and_fill_prompt(page, prompt)

        # Requirement 3: Aspect ratio check (bottom bar only, never top toolbar)
        await self._ensure_portrait_aspect_ratio(page)

        # Requirement 4: Submit prompt
        await self._submit_prompt(page)
        logger.info(
            f"[FlowCDP] Scene {scene_num} prompt submitted. "
            f"Waiting up to {self.scene_timeout_seconds}s for Flow generation & inspector download button..."
        )

        # Requirement 5: Download trigger & polling (up to 180s)
        start_ts = time.time()
        downloaded_path: Optional[Path] = None
        clicked_video_to_open_inspector = False

        while time.time() - start_ts < self.scene_timeout_seconds:
            await page.wait_for_timeout(2500)

            # A. Check if a file already landed in Downloads directory
            new_dl = self._find_new_download_in_dir(downloads_baseline, submit_timestamp)
            if new_dl and new_dl.exists() and new_dl.stat().st_size > 1000:
                if new_dl.resolve() != target_download_file.resolve():
                    shutil.copy2(str(new_dl), str(target_download_file))
                downloaded_path = target_download_file
                logger.info(f"[FlowCDP] Detected downloaded file in Downloads folder -> {target_download_file}")
                break

            # B. Check if generation has completed (new <video> ready or inspector download button active)
            curr_video_count = await page.locator("video").count()
            curr_video_srcs = await page.evaluate(
                """() => Array.from(document.querySelectorAll('video'))
                    .map(v => v.currentSrc || v.src || '')
                    .filter(Boolean)"""
            )
            new_srcs = [s for s in curr_video_srcs if s not in prev_video_srcs]
            video_finished = curr_video_count > prev_video_count or len(new_srcs) > 0

            # Also check if the inspector download button appeared directly after submission
            dl_btn = await self._find_inspector_download_button(page) if (video_finished or (time.time() - start_ts > 8)) else None

            if video_finished and dl_btn is None and not clicked_video_to_open_inspector:
                # Click the completed video tile once to open the right-hand detail inspector panel
                try:
                    newest_video = page.locator("video").first
                    if await newest_video.is_visible():
                        await newest_video.click(timeout=2000)
                        clicked_video_to_open_inspector = True
                        await page.wait_for_timeout(600)
                        dl_btn = await self._find_inspector_download_button(page)
                except Exception:
                    pass

            if dl_btn is not None:
                logger.info(f"[FlowCDP] Located completed video download button in inspector panel for Scene {scene_num}. Clicking now...")
                try:
                    async with page.expect_download(timeout=20000) as download_info:
                        await dl_btn.click(timeout=4000)
                    download = await download_info.value
                    await download.save_as(str(target_download_file))
                    downloaded_path = target_download_file
                    logger.info(f"[FlowCDP] Downloaded Scene {scene_num} via inspector download button -> {target_download_file}")
                    break
                except Exception as dl_err:
                    logger.debug(f"[FlowCDP] Inspector download click note (checking Downloads dir next): {dl_err}")
                    await page.wait_for_timeout(2000)
                    new_dl = self._find_new_download_in_dir(downloads_baseline, submit_timestamp)
                    if new_dl and new_dl.exists() and new_dl.stat().st_size > 1000:
                        if new_dl.resolve() != target_download_file.resolve():
                            shutil.copy2(str(new_dl), str(target_download_file))
                        downloaded_path = target_download_file
                        logger.info(f"[FlowCDP] Captured Scene {scene_num} from Downloads dir after inspector click -> {target_download_file}")
                        break

            if video_finished:
                # Direct <video src> stream fallback if download button used blob/direct URL
                target_src = new_srcs[0] if new_srcs else (curr_video_srcs[0] if curr_video_srcs else None)
                if target_src and target_src.startswith("http"):
                    try:
                        req = urllib.request.Request(target_src, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=30) as resp:
                            data = resp.read()
                            if len(data) > 1000:
                                target_download_file.write_bytes(data)
                                downloaded_path = target_download_file
                                logger.info(f"[FlowCDP] Saved Scene {scene_num} from video src -> {target_download_file}")
                                break
                    except Exception as src_err:
                        logger.debug(f"[FlowCDP] Direct video src download note: {src_err}")

        if not downloaded_path or not downloaded_path.exists() or downloaded_path.stat().st_size < 1000:
            raise TimeoutError(
                f"Google Flow scene {scene_num} generation timed out after {self.scene_timeout_seconds}s on {page.url}."
            )

        if output_path and output_path.resolve() != downloaded_path.resolve():
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(downloaded_path), str(output_path))
            return output_path

        return downloaded_path

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
