import os
import tempfile
import unittest
from unittest.mock import patch

import pulsar as P
import pulsar_gui as G
import pulsar_macros as M


class FakeDevice:
    def __init__(self, pid=0x002E):
        self.pid = pid
        self.profile = 1
        self.profile_writes = 0
        self.button_writes = 0
        self.last_matrix = None
        self.normalise_profile_writes = False
        block = bytearray(P.BLOCK_LEN)
        block[0] = 0x64
        block[1] = 0x01
        block[2] = P.CODE_FROM_RATE[500]
        block[3] = (1 << 4) | 7
        block[0x3D] = 2  # steady
        block[0x40] = 0xF0
        block[0x90] = 0xA5
        block[0x91] = 0
        self.report = bytearray(P.DATA_LEN)
        self.report[0] = P.DATA_ID
        self.report[P.HDR_LEN:P.HDR_LEN + P.BLOCK_LEN] = block

    def active_profile(self):
        return self.profile

    def read_profile_raw(self, profile):
        return bytes(self.report)

    def write_profile(self, profile, block):
        self.profile_writes += 1
        self.report[P.HDR_LEN:P.HDR_LEN + P.BLOCK_LEN] = bytes(block)
        if self.normalise_profile_writes:
            # Simulate firmware normalisation or a bad read-back.
            self.report[P.HDR_LEN + 2] ^= 0x01

    def write_buttons(self, profile, matrix):
        self.button_writes += 1
        self.last_matrix = list(matrix)

    def cmd(self, command):
        return bytes([P.CMD_ID, 0, 0x10, 87, 0, 0, 0, 0])


class ProtocolTests(unittest.TestCase):
    def test_dpi_register_round_trip(self):
        for dpi in range(P.DPI_MIN, P.DPI_MAX + 1, P.DPI_STEP):
            self.assertEqual(P.dpi_from_reg(P.reg_from_dpi(dpi)), dpi)
        with self.assertRaises(ValueError):
            P.reg_from_dpi(1550)
        with self.assertRaises(ValueError):
            P.reg_from_dpi(16100)

    def test_patch_rate_preserves_unrelated_flag_bits(self):
        block = bytearray(P.BLOCK_LEN)
        block[2] = 0xA0 | P.CODE_FROM_RATE[125]
        P.patch_rate(block, 1000)
        self.assertEqual(block[2], 0xA0 | P.CODE_FROM_RATE[1000])

    def test_patch_dpi_updates_x_and_y_only_when_independent_flag_is_set(self):
        block = bytearray(P.BLOCK_LEN)
        block[2] = 0x80 | P.CODE_FROM_RATE[500]
        P.patch_dpi(block, 3, 2400)
        expected = P.reg_from_dpi(2400)
        self.assertEqual(int.from_bytes(block[9:11], "little"), expected)
        self.assertEqual(int.from_bytes(block[0x19:0x1B], "little"), expected)
        block[2] &= 0x7F
        P.patch_dpi(block, 3, 1600)
        self.assertEqual(int.from_bytes(block[9:11], "little"), P.reg_from_dpi(1600))
        self.assertEqual(int.from_bytes(block[0x19:0x1B], "little"), expected)

    def test_default_button_matrix_has_required_left_click(self):
        matrix = P.default_matrix()
        self.assertEqual(len(matrix), P.N_BTN)
        self.assertIn(P.BTN_NAMED["left"], matrix)

    def test_button_parser_round_trips_simple_values(self):
        for value in ("left", "right", "middle", "back", "forward", "disable", "key:ctrl+c", "media:mute"):
            parsed = P.parse_button(value)
            self.assertIsInstance(parsed, int)
            self.assertGreaterEqual(parsed, 0)
        self.assertEqual(P.parse_button("raw:11010000"), 0x00000111)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"HOME": self.home.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.home.cleanup)
        self.device = FakeDevice()
        self.session = G.Session(self.device)
        self.session.load()

    def want(self):
        source = self.session.orig
        return {
            "rate": source["rate"], "n_stages": source["n_stages"], "cur_stage": source["cur_stage"],
            "dpi": list(source["dpi"]), "colors": list(source["colors"]), "led": dict(source["led"]),
        }

    def test_invalid_button_matrix_is_rejected_before_any_hardware_write(self):
        want = self.want()
        want["rate"] = 1000
        matrix = P.default_matrix()
        matrix = [x for x in matrix]
        matrix[0] = P.BTN_NAMED["right"]
        with self.assertRaisesRegex(ValueError, "left"):
            self.session.apply(want, matrix)
        self.assertEqual(self.device.profile_writes, 0)
        self.assertEqual(self.device.button_writes, 0)

    def test_apply_preserves_profile_bytes_changed_after_load(self):
        # Simulate the mouse/vendor app updating a field that this UI does not edit.
        self.device.report[P.HDR_LEN + 0x91] = 0x55
        want = self.want()
        want["rate"] = 1000
        self.session.apply(want)
        self.assertEqual(self.device.report[P.HDR_LEN + 0x91], 0x55)

    def test_profile_write_is_backed_up_and_read_back(self):
        want = self.want()
        want["rate"] = 1000
        result = self.session.apply(want)
        self.assertIn("read-back: matches", result)
        self.assertEqual(self.device.profile_writes, 1)
        backup_dir = os.path.join(self.home.name, ".config", "pulsar-linux")
        backups = [name for name in os.listdir(backup_dir) if name.endswith(".bin")]
        self.assertEqual(len(backups), 1)

    def test_profile_readback_mismatch_skips_button_write(self):
        self.device.normalise_profile_writes = True
        want = self.want()
        want["rate"] = 1000
        matrix = list(self.session.matrix)
        matrix[1] = P.BTN_NAMED["middle"]
        result = self.session.apply(want, matrix)
        self.assertIn("button changes were skipped for safety", result)
        self.assertEqual(self.device.profile_writes, 1)
        self.assertEqual(self.device.button_writes, 0)

    def test_changed_button_map_is_backed_up_and_remembered(self):
        want = self.want()
        matrix = list(self.session.matrix)
        matrix[1] = P.BTN_NAMED["middle"]
        result = self.session.apply(want, matrix)
        self.assertIn("button matrix written", result)
        self.assertEqual(self.device.button_writes, 1)
        self.assertEqual(self.device.last_matrix, matrix)
        self.assertTrue(self.session.matrix_known)
        files = os.listdir(os.path.join(self.home.name, ".config", "pulsar-linux"))
        self.assertTrue(any(name.startswith("buttons-backup-") for name in files))
        self.assertTrue(any(name == "buttons-p1.json" for name in files))


class MacroTests(unittest.TestCase):
    def test_script_parser_accepts_safe_basic_steps(self):
        steps = M.parse_script("# comment\nkey ctrl+c\ntype Hi!\nwait 100")
        self.assertEqual(steps[0], ("tap", ["LEFTCTRL", "C"]))
        self.assertEqual(steps[-1], ("wait", 100))

    def test_script_parser_rejects_excessive_wait_and_size(self):
        with self.assertRaisesRegex(ValueError, "600000"):
            M.parse_script("wait 600001")
        with self.assertRaisesRegex(ValueError, "too large"):
            M.parse_script("x" * 64001)

    def test_trigger_allocator_returns_unique_function_key(self):
        self.assertEqual(M.free_trigger({}), "f13")
        full = {str(i): {"trigger": key} for i, key in enumerate(M.TRIGGERS)}
        with self.assertRaisesRegex(ValueError, "no free trigger"):
            M.free_trigger(full)


if __name__ == "__main__":
    unittest.main()
