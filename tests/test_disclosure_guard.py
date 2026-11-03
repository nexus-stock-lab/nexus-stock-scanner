import unittest
from datetime import datetime
from disclosure_guard import check_disclosures

class DisclosureGuardTests(unittest.TestCase):
    def test_cutoff_and_review_boundary(self):
        data={'cik':1,'filings':{'recent':{'accessionNumber':['old','new','later'],
            'acceptanceDateTime':['2026-11-03T13:00:00Z','2026-11-03T14:45:00Z','2026-11-03T14:46:00Z'],
            'form':['10-Q','8-K','8-K'],'reportDate':['2026-09-30']*3}}}
        at=datetime.fromisoformat('2026-11-03T23:45:00+09:00')
        r=check_disclosures(data,1,'2026-11-03T13:00:00Z',at)
        self.assertEqual(r['new_accessions'],['new'])
        self.assertFalse(r['may_register'])
        r=check_disclosures(data,1,'2026-11-03T14:45:00Z',at)
        self.assertEqual(r['status'],'NO_NEW_SEC_FILINGS_OBSERVED')
        self.assertFalse(r['ir_coverage_verified'])
        self.assertEqual(check_disclosures(data,2,'2026-11-03T13:00:00Z',at)['status'],
                         'BLOCKED_INVALID_DISCLOSURE_EVIDENCE')
