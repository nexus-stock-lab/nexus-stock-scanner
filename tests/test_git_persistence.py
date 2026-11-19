import unittest
from subprocess import CompletedProcess, CalledProcessError
from unittest.mock import patch

from git_persistence import publish


class PublicationTests(unittest.TestCase):
    def test_race_rebases_committed_outputs_and_retries_without_force(self):
        calls=[]
        pushes=iter([CompletedProcess([],1,'','! [rejected] HEAD -> main (fetch first)'),
                     CompletedProcess([],0,'','')])
        def invoke(*args, **kw):
            calls.append(args)
            if args[0]=='push':return next(pushes)
            return CompletedProcess([],0,'saved-commit\n','')
        with patch('git_persistence.git',side_effect=invoke):
            self.assertEqual(publish('main',pause=lambda _:None),'saved-commit')
        self.assertEqual(sum(c[0]=='pull' for c in calls),2)
        self.assertEqual(sum(c[0]=='push' for c in calls),2)
        self.assertFalse(any('force' in arg for call in calls for arg in call))

    def test_permission_failure_is_not_retried(self):
        def invoke(*args,**kw):
            return CompletedProcess([],1,'','HTTP 403') if args[0]=='push' else CompletedProcess([],0,'','')
        with patch('git_persistence.git',side_effect=invoke) as mocked:
            with self.assertRaisesRegex(RuntimeError,'not a fast-forward race'):
                publish('main',pause=lambda _:self.fail('no retry'))
        self.assertEqual(sum(c.args[0]=='push' for c in mocked.call_args_list),1)

    def test_rebase_conflict_stops_before_push(self):
        def invoke(*args,**kw):
            if args[0]=='pull':raise CalledProcessError(1,args)
            return CompletedProcess([],0,'','')
        with patch('git_persistence.git',side_effect=invoke) as mocked:
            with self.assertRaises(CalledProcessError):publish('main')
        self.assertFalse(any(c.args[0]=='push' for c in mocked.call_args_list))

    def test_retry_budget_is_bounded(self):
        def invoke(*args,**kw):
            return CompletedProcess([],1,'','[rejected]') if args[0]=='push' else CompletedProcess([],0,'','')
        with patch('git_persistence.git',side_effect=invoke) as mocked:
            with self.assertRaisesRegex(RuntimeError,'bounded retries'):
                publish('main',pause=lambda _:None)
        self.assertEqual(sum(c.args[0]=='push' for c in mocked.call_args_list),3)
