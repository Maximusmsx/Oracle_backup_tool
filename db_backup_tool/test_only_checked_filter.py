import os
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
import main


class _WindowTestCase(unittest.TestCase):
    """共用装配；本身不含用例，具体用例放在下面的子类里。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = patch.object(main, "CONFIG_FILE", os.path.join(self.temp.name, "config.json"))
        self.config.start()
        self.window = main.MainWindow()
        self.window.profiles = {
            "A": {"user": "A", "password": "test", "conn_str": "db-a",
                  "export_dir": self.temp.name, "table_exports": []},
        }
        self.window._populate_combo()
        self.window._load_profile("A")

    def tearDown(self):
        self.window.close()
        self.config.stop()
        self.temp.cleanup()

    # ── helpers ───────────────────────────────────────
    def type_item(self, oracle_type="TABLE"):
        root = self.window.tree.invisibleRootItem()
        for i in range(root.childCount()):
            item = root.child(i)
            if item.data(0, Qt.ItemDataRole.UserRole) == oracle_type:
                return item
        raise AssertionError("type %s not in tree" % oracle_type)

    def load_type(self, oracle_type, names):
        """Expand a type row without touching a real database."""
        w = self.window
        db = Mock(use_thick=False)
        db.is_connected.return_value = True
        db.get_objects_by_type.return_value = names
        w.db_manager = db
        item = self.type_item(oracle_type)
        w.on_item_expanded(item)
        return item

    def children(self, item):
        root = item
        return [root.child(i) for i in range(root.childCount())
                if root.child(i).data(0, Qt.ItemDataRole.UserRole) != main.PLACEHOLDER_DATA]

    def visible_names(self, item):
        return [c.text(0) for c in self.children(item) if not c.isHidden()]

    def check(self, item, name):
        for child in self.children(item):
            if child.text(0) == name:
                child.setCheckState(0, Qt.CheckState.Checked)
                return child
        raise AssertionError("%s not found" % name)

    def states(self, item):
        return {c.text(0): c.checkState(0) for c in self.children(item)}


class OnlyCheckedFilterTest(_WindowTestCase):
    # ── display behaviour ─────────────────────────────
    def test_only_checked_hides_unchecked_children(self):
        w = self.window
        item = self.load_type("TABLE", ["A", "B", "C"])
        self.check(item, "A")
        self.check(item, "C")
        self.assertEqual(self.visible_names(item), ["A", "B", "C"])

        w.chk_only_checked.setChecked(True)
        self.assertEqual(self.visible_names(item), ["A", "C"])

        w.chk_only_checked.setChecked(False)
        self.assertEqual(self.visible_names(item), ["A", "B", "C"])

    def test_type_rows_and_placeholders_stay_visible(self):
        w = self.window
        item = self.load_type("TABLE", ["A", "B"])
        self.check(item, "A")
        w.chk_only_checked.setChecked(True)

        # 类型行永不隐藏，否则整类勾选后就再也找不回来了
        self.assertFalse(item.isHidden())
        self.assertFalse(item.child(0).isHidden())   # 占位符提示仍在
        empty = self.type_item("VIEW")
        self.assertFalse(empty.isHidden())

    def test_unchecking_hides_the_child_immediately(self):
        w = self.window
        item = self.load_type("TABLE", ["A", "B"])
        self.check(item, "A")
        self.check(item, "B")
        w.chk_only_checked.setChecked(True)
        self.assertEqual(self.visible_names(item), ["A", "B"])

        child = self.check(item, "B")
        child.setCheckState(0, Qt.CheckState.Unchecked)
        self.assertEqual(self.visible_names(item), ["A"])

    def test_count_label_tracks_checked_children_and_whole_types(self):
        w = self.window
        item = self.load_type("TABLE", ["A", "B", "C"])
        self.assertEqual(w.lbl_checked_count.text(), "已勾选 0 个")

        self.check(item, "A")
        self.check(item, "B")
        self.assertEqual(w.lbl_checked_count.text(), "已勾选 2 个")

        # 勾选类型行会把子对象一并勾上，计数随之变为 3
        item.setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual(w.lbl_checked_count.text(), "已勾选 3 个")

    def test_whole_type_selection_is_counted_separately(self):
        w = self.window
        item = self.load_type("TABLE", ["A", "B", "C"])
        self.check(item, "A")
        self.assertEqual(w.lbl_checked_count.text(), "已勾选 1 个")

        # 未展开但整类勾选的类型，单独标注而不是混入具体个数
        self.type_item("VIEW").setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual(w.lbl_checked_count.text(), "已勾选 1 个（另有 1 类整类勾选）")
        self.assertIsNone(w._collect_export_targets()["VIEW"])

    # ── interaction with the existing name filter ─────
    def test_name_filter_and_checked_filter_compose(self):
        w = self.window
        item = self.load_type("TABLE", ["AB_ONE", "AB_TWO", "CD_THREE"])
        self.check(item, "AB_TWO")
        w.chk_only_checked.setChecked(True)
        self.assertEqual(self.visible_names(item), ["AB_TWO"])

        w.filter_input.setText("ab_")
        w.apply_filter()
        self.assertEqual(self.visible_names(item), ["AB_TWO"])

        self.check(item, "AB_ONE")
        self.assertEqual(self.visible_names(item), ["AB_ONE", "AB_TWO"])

        w.filter_input.setText("cd_")
        w.apply_filter()
        # 两个条件是“与”的关系：名称要匹配 cd_，且必须已勾选
        self.assertEqual(self.visible_names(item), [])
        self.check(item, "CD_THREE")
        self.assertEqual(self.visible_names(item), ["CD_THREE"])

    # ── selection semantics must not change ───────────
    def test_checking_type_row_still_selects_every_hidden_child(self):
        w = self.window
        item = self.load_type("TABLE", ["A", "B", "C"])
        self.check(item, "A")
        w.chk_only_checked.setChecked(True)
        self.assertEqual(self.visible_names(item), ["A"])

        item.setCheckState(0, Qt.CheckState.Checked)
        states = self.states(item)
        self.assertEqual(set(states.values()), {Qt.CheckState.Checked})
        self.assertEqual(self.visible_names(item), ["A", "B", "C"])

    def test_export_targets_are_identical_with_and_without_the_filter(self):
        w = self.window
        item = self.load_type("TABLE", ["A", "B", "C"])

        self.check(item, "A")
        self.check(item, "C")
        self.assertEqual(w._collect_export_targets(), {"TABLE": ["A", "C"]})
        w.chk_only_checked.setChecked(True)
        self.assertEqual(w._collect_export_targets(), {"TABLE": ["A", "C"]})

        # 整类勾选应仍然导出全部，而不是退化成“仅可见项”
        item.setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual(w._collect_export_targets(), {"TABLE": None})
        w.chk_only_checked.setChecked(False)
        self.assertEqual(w._collect_export_targets(), {"TABLE": None})

    def test_selections_survive_toggling_the_filter(self):
        w = self.window
        item = self.load_type("TABLE", ["A", "B"])
        self.check(item, "A")
        before = dict(w._collect_selections())
        w.chk_only_checked.setChecked(True)
        self.assertEqual(w._collect_selections(), before)
        w.chk_only_checked.setChecked(False)
        self.assertEqual(w._collect_selections(), before)


class OnlyCheckedDataTabTest(_WindowTestCase):
    """同一复选框还要作用于表数据页。"""

    def setUp(self):
        super().setUp()
        self.window.tabs.setCurrentIndex(0)

    def load_tables(self, names):
        db = Mock(use_thick=False)
        db.connect.return_value = (True, "OK")
        db.is_connected.return_value = True
        db.get_objects_by_type.return_value = names
        with patch.object(main, "DatabaseManager", return_value=db):
            self.window.tabs.setCurrentIndex(1)
        return db

    def visible_tables(self):
        grid = self.window.table_data_grid
        return [grid.item(r, 0).text() for r in range(grid.rowCount())
                if not grid.isRowHidden(r)]

    def check_table(self, name):
        grid = self.window.table_data_grid
        for r in range(grid.rowCount()):
            if grid.item(r, 0).text() == name:
                grid.item(r, 0).setCheckState(Qt.CheckState.Checked)
                return
        raise AssertionError("%s not found" % name)

    # ── 表数据页的显示行为 ───────────────────────────
    def test_data_tab_hides_unchecked_rows(self):
        w = self.window
        self.load_tables(["T_A", "T_B", "T_C"])
        self.assertEqual(w.table_data_grid.rowCount(), 3)
        self.check_table("T_B")

        w.chk_only_checked.setChecked(True)
        self.assertEqual(self.visible_tables(), ["T_B"])

        w.chk_only_checked.setChecked(False)
        self.assertEqual(self.visible_tables(), ["T_A", "T_B", "T_C"])

    def test_unchecking_a_row_hides_it_immediately(self):
        w = self.window
        self.load_tables(["T_A", "T_B"])
        self.check_table("T_A")
        self.check_table("T_B")
        w.chk_only_checked.setChecked(True)
        self.assertEqual(self.visible_tables(), ["T_A", "T_B"])

        for r in range(w.table_data_grid.rowCount()):
            if w.table_data_grid.item(r, 0).text() == "T_B":
                w.table_data_grid.item(r, 0).setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual(self.visible_tables(), ["T_A"])

    def test_data_tab_reports_when_nothing_is_checked(self):
        w = self.window
        self.load_tables(["T_A", "T_B"])
        w.chk_only_checked.setChecked(True)
        self.assertEqual(self.visible_tables(), [])
        self.assertIn("无勾选表", w.lbl_table_count.text())

        self.check_table("T_A")
        self.assertNotIn("无勾选表", w.lbl_table_count.text())

    def test_data_name_filter_and_checked_filter_compose(self):
        w = self.window
        self.load_tables(["AB_ONE", "AB_TWO", "CD_THREE"])
        self.check_table("AB_TWO")
        w.chk_only_checked.setChecked(True)
        self.assertEqual(self.visible_tables(), ["AB_TWO"])

        w.data_filter_input.setText("ab_")
        w.apply_data_filter()
        self.assertEqual(self.visible_tables(), ["AB_TWO"])

        self.check_table("AB_ONE")
        self.assertEqual(self.visible_tables(), ["AB_ONE", "AB_TWO"])

        w.data_filter_input.setText("cd_")
        w.apply_data_filter()
        self.assertEqual(self.visible_tables(), [])
        self.check_table("CD_THREE")
        self.assertEqual(self.visible_tables(), ["CD_THREE"])

    # ── 导出内容不受影响 ─────────────────────────────
    def test_hidden_data_rows_are_still_exported(self):
        w = self.window
        self.load_tables(["T_A", "T_B", "T_C"])
        self.check_table("T_A")
        grid = w.table_data_grid
        for r in range(grid.rowCount()):
            if grid.item(r, 0).text() == "T_A":
                grid.item(r, 1).setText("id > 3")
        before = w._collect_table_exports()
        self.assertEqual(before, [{"table": "T_A", "where": "id > 3"}])

        w.chk_only_checked.setChecked(True)
        self.assertEqual(w._collect_table_exports(), before)
        w.chk_only_checked.setChecked(False)
        self.assertEqual(w._collect_table_exports(), before)

    def test_filter_applies_while_loading_with_the_box_unchecked(self):
        w = self.window
        self.load_tables(["T_A", "T_B"])
        self.check_table("T_B")
        self.window.tabs.setCurrentIndex(0)
        w.chk_only_checked.setChecked(True)
        # 表清单还没加载，切换页签后加载时应直接套用当前复选框
        w.tabs.setCurrentIndex(1)
        self.assertEqual(self.visible_tables(), ["T_B"])

    def test_toggling_from_ddl_tab_does_not_connect_to_the_database(self):
        w = self.window
        with patch.object(main, "DatabaseManager") as factory:
            w.chk_only_checked.setChecked(True)
            factory.assert_not_called()
        self.assertEqual(w.lbl_table_count.text(),
                         "表清单尚未加载；打开表数据页或点击刷新自动加载")

    def test_both_tabs_share_one_checkbox(self):
        w = self.window
        item = self.load_type("TABLE", ["D_A", "D_B"])
        self.check(item, "D_A")
        self.load_tables(["T_A", "T_B"])
        self.check_table("T_B")

        w.chk_only_checked.setChecked(True)
        self.assertEqual(self.visible_names(item), ["D_A"])
        self.assertEqual(self.visible_tables(), ["T_B"])

        w.chk_only_checked.setChecked(False)
        self.assertEqual(self.visible_names(item), ["D_A", "D_B"])
        self.assertEqual(self.visible_tables(), ["T_A", "T_B"])


if __name__ == "__main__":
    unittest.main()