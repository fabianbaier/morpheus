import random
import unittest

from morpheus import db, rain


def _visible_ratio(rendered) -> float:
    cells = [ch for ch in rendered.plain if ch != "\n"]
    if not cells:
        return 0.0
    return sum(1 for ch in cells if ch != " ") / len(cells)


class RainTest(unittest.TestCase):
    def test_initial_rain_has_ambient_texture(self) -> None:
        random.seed(7)
        matrix = rain.Rain(cols=60, rows=20)

        self.assertGreater(_visible_ratio(matrix.render()), 0.10)

    def test_working_rain_stays_dense_after_ticks(self) -> None:
        random.seed(11)
        matrix = rain.Rain(cols=60, rows=20)
        matrix.update_missions([
            db.Mission(
                tab_id="9",
                goal="build dense rain",
                state="working",
                updated_at=1,
            )
        ])

        for _ in range(12):
            matrix.tick()

        self.assertGreater(_visible_ratio(matrix.render()), 0.12)

    def test_resize_fills_new_columns_immediately(self) -> None:
        random.seed(13)
        matrix = rain.Rain(cols=20, rows=10)

        matrix.resize(cols=60, rows=20)

        self.assertEqual(len(matrix.columns), 60)
        self.assertGreater(_visible_ratio(matrix.render()), 0.10)


if __name__ == "__main__":
    unittest.main()
