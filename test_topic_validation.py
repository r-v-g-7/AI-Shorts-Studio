import urllib.request
import urllib.error
import json

BASE_URL = "http://127.0.0.1:8000"

# Realistic cinematic story description (> 1,500 characters)
LONG_CINEMATIC_STORY = """In the pitch-black silence of the Mariana Trench, nearly eleven kilometers beneath the ocean surface, an unexpected phenomenon defies the laws of deep-sea biology. At depths where the hydrostatic pressure exceeds one thousand atmospheres—enough to crush a reinforced steel submarine like an aluminum soda can—biologists using autonomous deep-submergence rovers discovered a massive cathedral of hydrothermal spires emitting an eerie, continuous sapphire-blue luminescence.

Unlike standard biological photophores found on anglerfish or viperfish that blink sporadically to lure prey, this vast field of bioluminescence pulses with a synchronized mathematical rhythm. Massive, semi-translucent organisms with crystalline exoskeleton plating hover in the thermal updrafts, exchanging rhythmic waves of light across hundreds of meters of abyssal water. Microscopic probe scans reveal that their tissues contain an unknown organometallic compound that converts deep earth geothermal radiation directly into visible light, completely independent of solar photosynthesis.

As the ROV drifted closer to the central thermal vent, its high-definition macro probe lenses captured intricate cutaway structures: delicate internal chambers where glowing mineral fluids circulate through nano-scale silicate conduits. The creature turned slowly toward the camera, its iridescent cyan ocular receptors reflecting the vehicle's illumination, revealing an ancient evolutionary branch that has thrived in absolute isolation for over two hundred million years."""

print(f"Test Story Length: {len(LONG_CINEMATIC_STORY)} characters")
assert len(LONG_CINEMATIC_STORY) > 1000, "Test story must be > 1000 characters"


def test_post_long_topic():
    print("\n--- Test 1: POST /api/jobs with detailed story (>1,000 chars) ---")
    payload = {
        "title": "Abyssal Cathedral Luminescence",
        "topic": LONG_CINEMATIC_STORY,
        "visual_style": "deep_dive_3d"
    }
    
    req = urllib.request.Request(
        f"{BASE_URL}/api/jobs",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    
    with urllib.request.urlopen(req) as response:
        assert response.status == 201
        data = json.loads(response.read().decode("utf-8"))
        job_id = data["id"]
        print(f"[PASS] Job Created Successfully: ID={job_id}")
        assert data["topic"] == LONG_CINEMATIC_STORY
        assert len(data["topic"]) == len(LONG_CINEMATIC_STORY)
        print(f"[PASS] Returned topic matches exact length: {len(data['topic'])} characters")

    # Step 2: Test script generation with the long topic
    print("\n--- Test 2: Downstream Script Generation with long topic ---")
    script_req = urllib.request.Request(
        f"{BASE_URL}/api/jobs/{job_id}/generate-script",
        data=json.dumps({}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(script_req) as script_res:
        assert script_res.status == 200
        script_data = json.loads(script_res.read().decode("utf-8"))
        assert script_data["status"] == "success"
        scenes = script_data["script"]["scenes"]
        print(f"[PASS] Downstream Script Engine Generated {len(scenes)} scenes without validation error!")


def test_topic_upper_bound_rejection():
    print("\n--- Test 3: Upper Bound Enforcement (>10,000 chars) ---")
    excessive_topic = "A" * 10005
    payload = {
        "title": "Too Large Topic",
        "topic": excessive_topic,
        "visual_style": "deep_dive_3d"
    }
    req = urllib.request.Request(
        f"{BASE_URL}/api/jobs",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req)
        assert False, "Should have failed with 422"
    except urllib.error.HTTPError as e:
        assert e.code == 422
        body = json.loads(e.read().decode("utf-8"))
        print(f"[PASS] Successfully rejected payload >10,000 chars with HTTP 422: {body['detail'][0]['msg']}")


def test_topic_min_length_rejection():
    print("\n--- Test 4: Minimum Length Enforcement (<5 chars) ---")
    payload = {
        "title": "Short Topic",
        "topic": "abc",
        "visual_style": "deep_dive_3d"
    }
    req = urllib.request.Request(
        f"{BASE_URL}/api/jobs",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req)
        assert False, "Should have failed with 422"
    except urllib.error.HTTPError as e:
        assert e.code == 422
        body = json.loads(e.read().decode("utf-8"))
        print(f"[PASS] Successfully rejected topic < 5 chars with HTTP 422: {body['detail'][0]['msg']}")


if __name__ == "__main__":
    test_post_long_topic()
    test_topic_upper_bound_rejection()
    test_topic_min_length_rejection()
    print("\n" + "=" * 60)
    print("ALL TOPIC VALIDATION TESTS PASSED (100%)")
    print("=" * 60)
