"""Content-addressed source snapshots. No model calls or production DB writes."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone

from .utils import stable_id

CONTRACT = 'source_archive_v1'


def file_hash(path):
    sha = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(chunk)
    return sha.hexdigest()


def archive_file(path: Path, root: Path):
    """Publish a fully written blob without replacing an existing archive object."""
    path = path.resolve(); root = root.resolve(); root.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.source-', dir=root)
    temporary = Path(temporary)
    try:
        with path.open('rb') as source, os.fdopen(fd, 'wb') as target:
            fd = None
            before = os.fstat(source.fileno())
            sha = hashlib.sha256(); size = 0
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                target.write(chunk); sha.update(chunk); size += len(chunk)
            target.flush(); os.fsync(target.fileno())
            after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != after.st_size:
            raise ValueError('归档读取期间源文件变化，请重试该试卷')
        fingerprint = sha.hexdigest()
        archive = root / fingerprint[:2] / (fingerprint + '.blob')
        archive.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Hard-link publication is atomic and fails if another worker won.
            os.link(temporary, archive)
        except FileExistsError:
            pass
        if archive.stat().st_size != size or file_hash(archive) != fingerprint:
            raise ValueError('已存在的内容归档损坏，禁止覆盖')
        return {'contract': CONTRACT, 'sha256': fingerprint, 'size_bytes': size,
                'archive_path': str(archive), 'original_path': str(path),
                'observed_at': datetime.now(timezone.utc).isoformat(),
                'binding': 'parsed_snapshot'}
    finally:
        if fd is not None: os.close(fd)
        temporary.unlink(missing_ok=True)


def snapshot_sample(sample, root, job_id):
    result = copy.deepcopy(sample)
    result['_source_snapshots'] = {kind: {**archive_file(Path(path), root), 'import_job_id': job_id}
        for kind, path in sample['components'].items()}
    result['component_hashes'] = {kind: row['sha256'] for kind, row in result['_source_snapshots'].items()}
    return result


def verify_source(source, *, verify_files=True):
    archive = source.get('archive')
    if not isinstance(archive, dict) or archive.get('contract') != CONTRACT:
        raise ValueError('缺少解析时原文归档；请重新暂存，不能以当前文件补冒充历史输入')
    sha = archive.get('sha256', '')
    if len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
        raise ValueError('归档哈希无效')
    if source.get('sha256') != sha or source['size_bytes'] != archive['size_bytes']:
        raise ValueError('归档与解析来源哈希或大小不一致')
    if str(Path(source['path']).resolve()) != archive['original_path'] or archive.get('binding') != 'parsed_snapshot':
        raise ValueError('归档来源路径或绑定类型不一致')
    if not archive.get('observed_at') or not archive.get('import_job_id'):
        raise ValueError('归档缺少观察时间或导入批次')
    if verify_files:
        path = Path(archive['archive_path'])
        if not path.is_file() or path.stat().st_size != archive['size_bytes'] or file_hash(path) != sha:
            raise ValueError('原文归档不存在或哈希不符')
    return archive


def validate_archives(dataset, *, required=True, verify_files=True):
    """Structural input errors block storage; model/teaching quality does not."""
    sources = dataset.get('source_files', [])
    errors = []
    if required and not sources and dataset.get('papers'):
        errors.append('没有来源文件，不能正式入库')
    for source in sources:
        if not required and not source.get('archive'):
            continue
        try:
            verify_source(source, verify_files=verify_files)
        except (ValueError, KeyError, OSError, TypeError) as exc:
            errors.append(f"{source.get('id', 'unknown')}: {exc}")
    by_id = {s['id']: s for s in sources}
    if len(by_id) != len(sources): errors.append('来源文件ID重复')
    if required:
        for paper in dataset.get('papers', []):
            components = paper.get('components', [])
            if not any(c.get('kind') == '真题' for c in components):
                errors.append(f"{paper['id']}: 缺少真题来源")
            for component in components:
                source = by_id.get(component['source_file_id'])
                if not source or source['paper_id'] != paper['id'] or source['kind'] != component['kind']:
                    errors.append(f"{paper['id']}: 来源组件绑定错配")
    return {'contract': CONTRACT, 'ready': not errors, 'source_count': len(sources),
            'verified_count': sum(bool(s.get('archive')) for s in sources), 'blockers': errors,
            'model_calls': 0, 'ai_semantic_acceptance_required': False}


def revision_row(source, parser_version):
    archive = verify_source(source, verify_files=False)
    return {'id': stable_id('source_revision', source['id'], archive['sha256'], parser_version, archive['import_job_id']),
            'source_file_id': source['id'], 'sha256': archive['sha256'],
            'archive_path': archive['archive_path'], 'size_bytes': archive['size_bytes'],
            'original_path': archive['original_path'], 'parser_version': parser_version,
            'import_job_id': archive['import_job_id'], 'observed_at': archive['observed_at'],
            'binding': archive['binding'], 'rights_status': 'unknown'}
