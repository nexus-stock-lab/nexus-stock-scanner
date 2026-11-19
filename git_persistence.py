"""Bounded fast-forward publication of already staged Actions outputs.

Never force-push, resolve conflicts, or regenerate research/plans on retry.
"""
import os
import subprocess
import time


def git(*args, check=True):
    return subprocess.run(['git', *args], check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def publish(branch, attempts=3, pause=time.sleep):
    if not branch or branch.startswith('-'):
        raise ValueError('explicit branch required')
    git('check-ref-format', 'refs/heads/'+branch)
    for attempt in range(attempts):
        # Other workflow outputs may still be unstaged. Preserve them while
        # replaying only the committed output; conflicts require human review.
        git('pull', '--rebase', '--autostash', 'origin', branch)
        pushed = git('push', 'origin', 'HEAD:refs/heads/'+branch, check=False)
        if pushed.returncode == 0:
            return git('rev-parse', 'HEAD').stdout.strip()
        error = pushed.stdout+pushed.stderr
        if not any(marker in error for marker in ('[rejected]', 'non-fast-forward', 'fetch first')):
            raise RuntimeError('GitHub publication failed; not a fast-forward race')
        if attempt+1 < attempts:
            pause(attempt+1)
    raise RuntimeError('GitHub publication still races after bounded retries; outputs not verified')


if __name__ == '__main__':
    print(publish(os.environ['BRANCH']))
