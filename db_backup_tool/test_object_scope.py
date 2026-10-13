import tempfile
import unittest
from unittest.mock import Mock, patch
import db_logic


class ObjectScopeTest(unittest.TestCase):
    def test_queries_only_current_user_objects(self):
        db = db_logic.DatabaseManager("lxd", "test", "test")
        cursor = Mock()
        cursor.fetchall.return_value = [("MY_OBJECT",)]
        db.conn = Mock()
        db.conn.cursor.return_value = cursor
        for _, kind in db_logic.OBJECT_CATEGORIES:
            self.assertEqual(db.get_objects_by_type(kind), ["MY_OBJECT"])
            sql = cursor.execute.call_args.args[0].lower()
            self.assertNotIn("dba_", sql)
            self.assertNotIn("all_", sql)
            self.assertIn("user_", sql)

    def test_old_cross_schema_selections_never_exported(self):
        db = Mock()
        db.connect.return_value = (True, "OK")
        db.get_objects_by_type.return_value = ["MINE"]
        db.get_ddl.return_value = "create table MINE (id number);"
        with tempfile.TemporaryDirectory() as folder, patch.object(db_logic, "DatabaseManager", return_value=db):
            db_logic.export_ddl_task(
                {"user": "lxd", "password": "test", "conn_str": "test"}, folder,
                {"PACKAGE": ["CWBS.GET_DAYS", "APEX_030200.APEXWS", "LXD.MINE"]}, Mock(), Mock(),
                table_exports=[{"table": "CWBS.OTHER"}, {"table": "LXD.MINE"}],
            )
            db.get_ddl.assert_called_once_with("PACKAGE", "MINE")
            self.assertEqual(db.export_table_data_sql.call_count, 1)
            self.assertEqual(db.export_table_data_sql.call_args.args[0], "MINE")


if __name__ == "__main__":
    unittest.main()
