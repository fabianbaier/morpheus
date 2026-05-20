import math
import unittest

from morpheus import intro


class IntroTest(unittest.TestCase):
    def test_geolocation_defaults_on_and_can_opt_out_with_env(self) -> None:
        options = intro.load_options(cfg={}, env={})
        self.assertTrue(options.enabled)
        self.assertTrue(options.geolocation)

        disabled = intro.load_options(cfg={}, env={"MORPHEUS_INTRO_GEO": "0"})
        self.assertTrue(disabled.enabled)
        self.assertFalse(disabled.geolocation)

    def test_intro_can_be_disabled_with_env(self) -> None:
        options = intro.load_options(cfg={}, env={"MORPHEUS_NO_INTRO": "1"})

        self.assertFalse(options.enabled)

    def test_manual_location_override_parses_label(self) -> None:
        options = intro.load_options(
            cfg={},
            env={"MORPHEUS_INTRO_LOCATION": "37.7749,-122.4194,San Francisco"},
        )

        self.assertIsNotNone(options.location)
        assert options.location is not None
        self.assertAlmostEqual(options.location.latitude, 37.7749)
        self.assertAlmostEqual(options.location.longitude, -122.4194)
        self.assertEqual(options.location.label, "San Francisco")

    def test_intro_duration_is_clamped_to_cinematic_window(self) -> None:
        short = intro.load_options(cfg={}, env={"MORPHEUS_INTRO_SECONDS": "2"})
        long = intro.load_options(cfg={}, env={"MORPHEUS_INTRO_SECONDS": "30"})

        self.assertEqual(short.duration_seconds, intro.MIN_INTRO_SECONDS)
        self.assertEqual(long.duration_seconds, intro.MAX_INTRO_SECONDS)

    def test_default_intro_duration_is_longer_cinematic_boot(self) -> None:
        options = intro.load_options(cfg={}, env={})

        self.assertEqual(options.duration_seconds, 7.5)

    def test_project_location_returns_visible_screen_point(self) -> None:
        point = intro.project_location(0, 0, 0, width=46, height=20)

        self.assertIsNotNone(point)
        assert point is not None
        x, y = point
        self.assertGreaterEqual(x, 0)
        self.assertLess(x, 46)
        self.assertGreaterEqual(y, 0)
        self.assertLess(y, 20)

    def test_project_location_hides_back_of_globe(self) -> None:
        self.assertIsNone(intro.project_location(0, 180, 0, width=46, height=20))

    def test_rotation_interpolation_locks_to_location_longitude(self) -> None:
        location = intro.IntroLocation(latitude=0, longitude=90)
        options = intro.IntroOptions(location=location)
        player = intro._IntroPlayer(options, width=80, height=24)

        rotation = player._rotation_for(0.82)

        normalized = (rotation + math.pi) % math.tau - math.pi
        self.assertAlmostEqual(normalized, math.radians(90), delta=0.2)

    def test_location_phase_zooms_and_centers_projected_location(self) -> None:
        location = intro.IntroLocation(latitude=37.7749, longitude=-122.4194)
        options = intro.IntroOptions(location=location)
        player = intro._IntroPlayer(options, width=100, height=32)
        rotation = player._rotation_for(0.92)

        globe_width, globe_height, left, top = player._globe_geometry(0.92, rotation)
        projected = intro.project_location(
            location.latitude,
            location.longitude,
            rotation,
            globe_width,
            globe_height,
        )

        self.assertGreater(globe_width, intro.BASE_GLOBE_WIDTH)
        self.assertGreater(globe_height, intro.BASE_GLOBE_HEIGHT)
        self.assertIsNotNone(projected)
        assert projected is not None
        screen_x = left + projected[0]
        screen_y = top + projected[1]
        self.assertAlmostEqual(screen_x, player.width // 2, delta=2)
        self.assertAlmostEqual(screen_y, player.height // 2, delta=2)

    def test_land_mask_places_california_on_west_coast(self) -> None:
        san_francisco_land = intro._is_land(math.radians(37.7749), math.radians(-122.4194))
        nearby_pacific = intro._is_land(math.radians(37.7749), math.radians(-127.0))

        self.assertTrue(san_francisco_land)
        self.assertFalse(nearby_pacific)


if __name__ == "__main__":
    unittest.main()
