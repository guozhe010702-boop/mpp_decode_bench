#!/usr/bin/env python3
"""Verified fixed-path deployment; never overwrite different files."""
import fcntl,hashlib,json,os,shutil,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parent
if sys.argv[1:] not in ([],['--check']): raise SystemExit('Usage: sudo bash install.sh [--check]')
if os.uname().machine!='aarch64' or os.geteuid()!=0: raise SystemExit('ARM64 Linux and root required')
manifest=json.loads((root/'manifest.json').read_text()); payload=root/'payload'
lock=open('/tmp/imx415-live-mpp-bench.lock','a+')
try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError: raise SystemExit('Camera program active. Stop before deployment.')
for unit in ('imx415-low-latency.service','imx415-latency-benchmark.service'):
    if subprocess.run(['systemctl','is-active','--quiet',unit]).returncode==0: raise SystemExit('Stop '+unit)
def digest(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''): h.update(b)
    return h.hexdigest()
conflicts=[]
for rel,r in manifest['files'].items():
    if not rel.startswith('userdata/av_dev/') or '..' in Path(rel).parts: raise SystemExit('Invalid path')
    source=payload/rel; target=Path('/')/rel
    if any(p.is_symlink() for p in target.parents): raise SystemExit('Symlinked destination parent: '+str(target))
    if r['type']=='symlink':
        if not source.is_symlink() or os.readlink(source)!=r['target']: raise SystemExit('Payload link mismatch: '+rel)
        same=target.is_symlink() and os.readlink(target)==r['target']
    else:
        if source.is_symlink() or not source.is_file() or digest(source)!=r['sha256']: raise SystemExit('Payload checksum mismatch: '+rel)
        same=not target.is_symlink() and target.is_file() and digest(target)==r['sha256']
    if (target.exists() or target.is_symlink()) and not same: conflicts.append(str(target))
if conflicts: raise SystemExit('Conflicting files; nothing installed. Back up/migrate first:\n'+'\n'.join(conflicts[:30]))
print('Verified '+str(len(manifest['files']))+' files/links; no conflicts.')
if '--check' in sys.argv: raise SystemExit(0)
for rel,r in manifest['files'].items():
    source=payload/rel; target=Path('/')/rel; target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists() or target.is_symlink(): continue
    if r['type']=='symlink': target.symlink_to(r['target'])
    else: shutil.copyfile(source,target); target.chmod(int(r['mode'],8))
print('Installed. No preview/autostart enabled. Next: bash check_board.sh')
