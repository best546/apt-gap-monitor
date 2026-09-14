import unittest
from collector.main import parse_items, summarize

XML = '''<response><body><items><item><aptNm>테스트아파트</aptNm><dealAmount>100,000</dealAmount><excluUseAr>84.91</excluUseAr><dealYear>2026</dealYear><dealMonth>9</dealMonth><dealDay>1</dealDay><floor>10</floor><dealingGbn>중개거래</dealingGbn><cdealDay></cdealDay></item></items></body></response>'''

class CollectorTest(unittest.TestCase):
    def test_parse_and_summary(self):
        rows = parse_items(XML)
        item = summarize({"id":"x","region":"테스트","name":"테스트아파트","aliases":[],"lawd_cd":"00000","area":84.9}, rows, 1.2)
        self.assertEqual(item["representative_manwon"], 100000)
        self.assertEqual(item["count"], 1)

if __name__ == "__main__": unittest.main()
