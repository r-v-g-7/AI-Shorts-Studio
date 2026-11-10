import asyncio
from backend.config import settings
from backend.database.connection import init_db
from backend.database.repository import JobRepository
from backend.schemas.job_manifest import JobStatus, VisualStyle
from backend.services.job_service import JobService
from backend.services.pipeline_controller import PipelineController
from backend.services.flow_automation_service import (
    FlowAutomationService,
    CDPConnectionError,
)


async def test_live_cdp_and_dom():
    print("=" * 80)
    print("PHASE 7 VERIFICATION: CHROME DEVTOOLS PROTOCOL (CDP) PLAYWRIGHT AUTOMATION")
    print("=" * 80)

    print("\n[1] Checking Configuration...")
    print(f"    CDP_DEBUGGING_URL:         {settings.CDP_DEBUGGING_URL}")
    print(f"    VIDEO_GENERATION_PROVIDER: {settings.VIDEO_GENERATION_PROVIDER}")
    assert settings.CDP_DEBUGGING_URL == "http://127.0.0.1:9222"
    assert settings.VIDEO_GENERATION_PROVIDER == "flow_browser"

    reachable = FlowAutomationService.is_cdp_reachable()
    print(f"    CDP Port 9222 Reachable:   {reachable}")

    service = FlowAutomationService()
    if reachable:
        print("\n[2] Connecting to live Chrome session on http://127.0.0.1:9222 via Playwright CDP...")
        try:
            state = await service.inspect_browser_state()
            print(f"[PASS] Connected to Chrome via CDP ({state['cdp_url']})!")
            print(f"[PASS] Open Tabs Detected: {state['open_tabs_count']}")
            for idx, tab in enumerate(state["open_tabs"], 1):
                print(f"       Tab {idx}: {tab['title']} -> {tab['url']}")

            flow_dom = state["flow_tab"]
            print("[PASS] Active Flow Tab DOM Validated:")
            print(f"       URL:              {flow_dom['url']}")
            print(f"       Title:            {flow_dom['title']}")
            print(f"       ReadyState:       {flow_dom['readyState']}")
            print(
                f"       ContentEditables: {flow_dom.get('contenteditables', 0)}, "
                f"Textareas: {flow_dom['textareas']}, Buttons: {flow_dom['buttons']}, Videos: {flow_dom['videos']}"
            )
            assert flow_dom["readyState"] in ("interactive", "complete")

            # Targeted DOM interaction test on the bottom prompt box
            page = service._page
            prompt_loc = await service._find_prompt_locator(page)
            assert prompt_loc is not None, "Expected bottom prompt locator to be found on active Flow project tab"
            if prompt_loc is not None:
                print("\n[2b] Testing Prompt Focus, Entry, and Popup Prevention on Live Flow Canvas...")
                sample_prompt = "9:16 vertical cinematic 3D Pixar style golden lion Sheru on a rooftop under the glowing moon"
                await service.focus_and_fill_prompt(page, sample_prompt)
                await service._ensure_portrait_aspect_ratio(page)

                entered = (await prompt_loc.inner_text() or "").strip()
                print(f"[PASS] Prompt Box Focused & Populated: '{entered}'")
                assert "Sheru" in entered, f"Expected sample prompt inside ProseMirror editor, got: '{entered}'"

                # Verify 'View mode' overlay was NOT accidentally opened
                view_mode_visible = False
                vm_loc = page.locator(':text("View mode")')
                if await vm_loc.count() > 0:
                    try:
                        view_mode_visible = await vm_loc.first.is_visible()
                    except Exception:
                        view_mode_visible = False
                assert not view_mode_visible, "ERROR: 'View mode' popup overlay was accidentally opened!"
                print("[PASS] Confirmed 'View mode' popup overlay is NOT open.")

                # Verify 'Agent' pill badge and bottom 'Start generation' button remain intact
                agent_btn = page.locator('button:has-text("Agent")').last
                if await agent_btn.count() > 0:
                    assert await agent_btn.is_visible(), "Expected 'Agent' pill button to remain visible"
                    print("[PASS] 'Agent' pill badge intact inside bottom prompt bar.")

                submit_btn = page.locator('button[aria-label="Start generation"], button:has-text("arrow_forward")').last
                if await submit_btn.count() > 0:
                    assert await submit_btn.is_visible(), "Expected bottom 'Start generation' button to be visible"
                    print("[PASS] Bottom 'Start generation' (arrow_forward) button located and ready.")

                # Clean up test text so the user's prompt input is left clean
                await prompt_loc.click()
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.wait_for_timeout(150)
                print("[PASS] Cleared test prompt text cleanly.")
        finally:
            await service.disconnect()
    else:
        print("\n[2] Port 9222 is not currently open; verifying clean CDPConnectionError diagnostic...")
        try:
            await service.connect_to_browser()
            assert False, "Expected CDPConnectionError when port 9222 is closed"
        except CDPConnectionError as e:
            assert "--remote-debugging-port=9222" in str(e)
            print(f"[PASS] Clean diagnostic raised: {e}")

    print("\n[3] Testing Pipeline Safe Fallback to AWAITING_CLIPS when CDP is unreachable...")
    init_db()
    job = PipelineController.create_new_job(
        topic="Pressure cooker POV comedy monologue in an Indian kitchen",
        title="Sentient Pressure Cooker Comedy",
        visual_style=VisualStyle.TALL_TALE_3D,
    )
    job_id = job["id"]
    script = PipelineController.execute_script_stage(job_id)
    print(f"[PASS] Sentient Objects Comedy Script Generated: '{script.title}'")
    print(f"       Hook: {script.hook}")
    assert "Pressure Cooker" in script.complete_narration or "seeti" in script.complete_narration.lower()

    orig_cdp = settings.CDP_DEBUGGING_URL
    try:
        settings.CDP_DEBUGGING_URL = "http://127.0.0.1:9299"
        clips = PipelineController.execute_video_generation_stage(
            job_id=job_id,
            provider_override="flow_browser",
        )
        assert clips == [], f"Expected empty clip list on fallback, got {clips}"
        updated_job = JobService.get_job(job_id)
        assert updated_job["status"] == JobStatus.AWAITING_CLIPS.value, (
            f"Expected job status AWAITING_CLIPS, got {updated_job['status']}"
        )
        logs = JobRepository.get_logs(job_id)
        assert any("--remote-debugging-port=9222" in (lg.get("message") or "") for lg in logs), (
            "Expected --remote-debugging-port=9222 launch instructions in job logs"
        )
        print(f"[PASS] Unreachable CDP safely placed job '{job_id[:8]}' into AWAITING_CLIPS with launch instructions logged!")
    finally:
        settings.CDP_DEBUGGING_URL = orig_cdp

    print("\n[4] Testing Auto-Resume Scene Skipping & Direct Jump to COMPOSITING...")
    from backend.services.scene_intake_service import SceneIntakeService
    from backend.services.video_generator import MockVideoGenerator

    incoming_dir = SceneIntakeService.get_incoming_directory(job_id)
    mock_gen = MockVideoGenerator()
    # Pre-populate scene_01.mp4 and scene_02.mp4 in incoming_clips/
    mock_gen.generate_scene_clip("scene 1", 3.0, incoming_dir / "scene_01.mp4", scene_index=0)
    mock_gen.generate_scene_clip("scene 2", 3.0, incoming_dir / "scene_02.mp4", scene_index=1)

    existing, missing = SceneIntakeService.get_existing_and_missing_scenes(job_id, total_scenes=4)
    assert existing == [0, 1] and missing == [2, 3], f"Expected existing=[0,1], missing=[2,3], got {existing}, {missing}"
    print(f"[PASS] Detected existing scenes {existing} in incoming_clips/ and missing scenes {missing}.")

    # Run video generation stage with mock provider -> should skip 1 & 2 and generate only 3 & 4
    clips = PipelineController.execute_video_generation_stage(job_id=job_id, provider_override="mock")
    assert len(clips) == 4
    logs = JobRepository.get_logs(job_id)
    assert any("skipping existing [scene_01.mp4, scene_02.mp4]" in (lg.get("message") or "") for lg in logs)
    print("[PASS] Auto-Resume skipped existing scenes [1, 2] and generated only missing scenes [3, 4]!")

    # Re-running when all 4 exist in incoming_clips/ must jump straight to COMPOSITING
    clips_again = PipelineController.execute_video_generation_stage(job_id=job_id, provider_override="flow_browser")
    assert len(clips_again) == 4
    assert JobService.get_job(job_id)["status"] == JobStatus.COMPOSITING.value
    print("[PASS] When all 4 scenes exist in incoming_clips/, pipeline jumps straight to COMPOSITING!")

    print("\n" + "=" * 80)
    print("ALL CDP CONNECTION & FLOW DOM INTERACTION CHECKS PASSED (100% SUCCESS)!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(test_live_cdp_and_dom())
