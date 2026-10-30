import unittest
from decimal import Decimal
from unittest.mock import patch

import broker_ytd_dispatch as dispatch
from project_fabric.insider_intelligence import (
    is_insider_disclosure,
    parse_disclosure,
)


class InsiderParserTests(unittest.TestCase):
    def test_recognizes_official_ownership_disclosure(self):
        self.assertTrue(is_insider_disclosure({
            "title": "Laporan Kepemilikan atau Setiap Perubahan Kepemilikan Saham Perusahaan Terbuka"
        }))
        self.assertFalse(is_insider_disclosure({"title": "Laporan Keuangan Interim"}))

    def test_parses_structured_buy(self):
        item = {
            "id": "announcement-1",
            "code": "BBRI",
            "title": "Laporan Kepemilikan Saham Direksi",
            "publishedAt": "2026-10-29T18:30:00",
            "attachments": [{"pdfUrl": "https://example.test/report.pdf"}],
        }
        text = """
Nama Pemilik Saham: Budi Santoso
Jabatan: Direktur
Jumlah Saham Sebelum Transaksi: 1.000.000
Jumlah Saham Yang Dibeli: 250.000
Harga Pembelian Per Saham: Rp4.500
Jumlah Saham Setelah Transaksi: 1.250.000
Tanggal Transaksi: 28 Oktober 2026
Tujuan Transaksi: Investasi pribadi
"""
        row = parse_disclosure(item, text)
        self.assertEqual(row["ticker"], "BBRI")
        self.assertEqual(row["side"], "BUY")
        self.assertEqual(row["shares"], Decimal("250000"))
        self.assertEqual(row["price"], Decimal("4500"))
        self.assertEqual(row["transaction_value"], Decimal("1125000000"))
        self.assertEqual(row["transaction_date"].isoformat(), "2026-10-28")
        self.assertEqual(row["parse_status"], "STRUCTURED")

    def test_infers_sell_from_before_after(self):
        item = {"id": "announcement-2", "code": "ENAK", "title": "Perubahan Kepemilikan Saham"}
        row = parse_disclosure(item, """
Jumlah saham sebelum transaksi: 2.000.000
Jumlah saham setelah transaksi: 1.500.000
""")
        self.assertEqual(row["side"], "SELL")
        self.assertEqual(row["shares"], Decimal("500000"))


class InsiderTelegramTests(unittest.TestCase):
    @patch("broker_ytd_dispatch.format_insider_report", return_value="INSIDER REPORT")
    def test_cache_only_route(self, formatter):
        self.assertEqual(dispatch.command_result("INSIDER BBRI", "123"), "INSIDER REPORT")
        formatter.assert_called_once_with("BBRI", refresh=False, limit=5)

    @patch("broker_ytd_dispatch.legacy.authorized", return_value=True)
    @patch("broker_ytd_dispatch.format_insider_report", return_value="REFRESHED")
    def test_owner_refresh_route(self, formatter, _authorized):
        self.assertEqual(dispatch.command_result("/insider REFRESH ENAK 3", "123"), "REFRESHED")
        formatter.assert_called_once_with("ENAK", refresh=True, limit=3)

    @patch("broker_ytd_dispatch.legacy.authorized", return_value=False)
    def test_refresh_rejects_non_owner(self, _authorized):
        self.assertIn("hanya tersedia", dispatch.command_result("INSIDER REFRESH BBRI", "group"))


if __name__ == "__main__":
    unittest.main()
