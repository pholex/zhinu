"""Application-owned content, references and revisions; never an execution log."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

MAX_BYTES = 16 * 1024 * 1024
MAX_TEXT = 40000
MAX_VERSIONS = 100
ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class ContentConflict(ValueError):
    pass


def identity(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("内容标识不正确")
    return value


def text(value, maximum=MAX_TEXT):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        raise ValueError("内容格式或长度不正确")
    return value


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError("内容字段不正确")
    return value


def ids(value, maximum=100):
    if not isinstance(value, list) or len(value) > maximum or len(set(map(str, value))) != len(value):
        raise ValueError("内容引用格式不正确")
    return [identity(item) for item in value]


def artifact_id(row):
    value = [row['session_id'], row['source'], row['artifact']['id']]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode('utf-8', errors='replace')).hexdigest()[:32]


def artifact_type(artifact):
    suffix = Path(str(artifact.get('path', ''))).suffix.lower()
    if suffix in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'}:
        return 'image'
    if suffix in {'.html', '.htm'}:
        return 'web'
    if suffix in {'.md', '.markdown', '.txt', '.csv', '.json', '.yaml', '.yml', '.xml'}:
        return 'document'
    return 'file'


def empty_state():
    return {'revision': 0, 'pages': {}, 'spaces': {}, 'projects': {}, 'favorites': {}, 'receipts': {}}


def validate_state(value):
    fields(value, empty_state())
    if type(value['revision']) is not int or value['revision'] < 0:
        raise ValueError('内容修订号无效')
    for group, maximum in [('pages', 300), ('spaces', 100), ('projects', 500), ('favorites', 3000), ('receipts', 3000)]:
        if not isinstance(value[group], dict) or len(value[group]) > maximum:
            raise ValueError('内容数量已到上限')
        for key in value[group]:
            identity(key)
    for page_id, page in value['pages'].items():
        fields(page, ['id', 'title', 'format', 'body', 'project_id', 'space_id', 'references', 'source', 'version', 'versions', 'deleted', 'created_at', 'updated_at'])
        validate_page(page, stored=True)
        if page['id'] != page_id:
            raise ValueError('页面身份与索引不一致')
        if type(page['deleted']) is not bool or type(page['version']) is not int or page['version'] < 1:
            raise ValueError('页面状态无效')
        if not isinstance(page['versions'], list) or not 1 <= len(page['versions']) <= MAX_VERSIONS:
            raise ValueError('页面版本数量无效')
        if page['version'] != len(page['versions']):
            raise ValueError('页面版本不连续')
        for index, version in enumerate(page['versions'], 1):
            fields(version, ['number', 'title', 'format', 'body', 'source', 'created_at'], ['project_id', 'space_id', 'references'])
            if version['number'] != index or not isinstance(version['format'], str) or version['format'] not in {'text', 'markdown', 'html'}:
                raise ValueError('历史版本无效')
            text(version['title'], 200); text(version['body']); text(version['source'], 1000)
    for space in value['spaces'].values():
        fields(space, ['id', 'title', 'description', 'project_ids', 'artifact_ids', 'updated_at'])
        identity(space['id']); text(space['title'], 200); text(space['description'], 5000)
        ids(space['project_ids']); ids(space['artifact_ids'])
    for project in value['projects'].values():
        validate_project(project)
    if any(type(flag) is not bool for flag in value['favorites'].values()):
        raise ValueError('收藏状态无效')
    for receipt in value['receipts'].values():
        fields(receipt, ['fingerprint', 'action', 'entity_id', 'revision'])
        text(receipt['fingerprint'], 64); text(receipt['action'], 40); identity(receipt['entity_id'])
    return value


def validate_page(value, *, stored=False):
    if not stored:
        fields(value, ['id', 'title', 'format', 'body', 'project_id', 'space_id', 'references', 'source', 'expected_version'])
        if type(value['expected_version']) is not int or value['expected_version'] < 0:
            raise ValueError('页面原版本无效')
    identity(value['id']); text(value['title'], 200); text(value['body']); text(value['source'], 1000)
    if not value['title'].strip() or not isinstance(value['format'], str) or value['format'] not in {'text', 'markdown', 'html'}:
        raise ValueError('页面名称或类型无效')
    for key in ['project_id', 'space_id']:
        if value[key] != '':
            identity(value[key])
    ids(value['references'])
    return value


def validate_project(value):
    fields(value, ['instructions', 'materials'])
    text(value['instructions'], 20000)
    if not isinstance(value['materials'], list) or len(value['materials']) > 30:
        raise ValueError('项目资料最多 30 条')
    seen = set()
    for material in value['materials']:
        fields(material, ['id', 'title', 'text', 'source'])
        identity(material['id']); text(material['title'], 200); text(material['text'], 20000); text(material['source'], 1000)
        if not material['title'].strip() or material['id'] in seen:
            raise ValueError('项目资料标题或标识无效')
        seen.add(material['id'])
    return value


class ContentStore:
    def __init__(self, path: Path, *, write_guard=None):
        self.path = Path(path)
        self.guard = write_guard or (lambda: None)
        self.lock = threading.RLock()

    def _read(self):
        try:
            with self.path.open('rb') as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('内容库超出容量')
            return validate_state(json.loads(raw.decode('utf-8', errors='replace')))
        except FileNotFoundError:
            return empty_state()

    def read(self):
        with self.lock:
            return self._read()

    def public(self):
        state = self.read()
        state.pop('receipts')
        return state

    def update(self, request, *, project_ids=(), artifact_ids=()):
        fields(request, ['revision', 'request_id', 'action', 'value'])
        rid = identity(request['request_id'])
        if type(request['revision']) is not int or request['revision'] < 0:
            raise ValueError('内容修订号无效')
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        with self.lock:
            current = self._read()
            prior = current['receipts'].get(rid)
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ContentConflict('同一请求标识不能用于不同内容')
                return {'state': {k: v for k, v in current.items() if k != 'receipts'}, 'receipt': prior, 'duplicate': True}
            if request['revision'] != current['revision']:
                raise ContentConflict('内容库已更新；原文保留，请读取当前版本后核对')
            saved = copy.deepcopy(current)
            action, value, now = text(request['action'], 40), request['value'], time.time()
            if action == 'page_save':
                validate_page(value)
                page_id = value['id']; prior_page = saved['pages'].get(page_id)
                if (prior_page['version'] if prior_page else 0) != value['expected_version']:
                    raise ContentConflict('页面已出现新版本；请核对后明确保存为另一版本')
                if prior_page and prior_page['deleted']:
                    raise ContentConflict('页面已移入回收区；请先恢复页面')
                self._references(value, project_ids, artifact_ids, saved)
                versions = copy.deepcopy(prior_page['versions']) if prior_page else []
                if len(versions) >= MAX_VERSIONS:
                    raise ValueError('此页面已达版本上限；请另建页面，旧历史保留')
                versions.append({'number': len(versions) + 1, 'title': value['title'], 'body': value['body'], 'format': value['format'], 'source': value['source'], 'created_at': now, 'project_id': value['project_id'], 'space_id': value['space_id'], 'references': copy.deepcopy(value['references'])})
                saved['pages'][page_id] = {k: copy.deepcopy(v) for k, v in value.items() if k != 'expected_version'}
                saved['pages'][page_id].update(version=len(versions), versions=versions, deleted=False, created_at=prior_page['created_at'] if prior_page else now, updated_at=now)
                entity_id = page_id
            elif action in {'page_delete', 'page_restore'}:
                fields(value, ['id']); entity_id = identity(value['id'])
                if entity_id not in saved['pages']:
                    raise ValueError('页面不存在')
                saved['pages'][entity_id]['deleted'] = action == 'page_delete'
                saved['pages'][entity_id]['updated_at'] = now
            elif action == 'space_save':
                fields(value, ['id', 'title', 'description', 'project_ids', 'artifact_ids'])
                entity_id = identity(value['id']); text(value['title'], 200); text(value['description'], 5000)
                if not value['title'].strip() or not set(ids(value['project_ids'])) <= set(project_ids) or not set(ids(value['artifact_ids'])) <= set(artifact_ids):
                    raise ValueError('空间名称或引用不属于当前工作台')
                saved['spaces'][entity_id] = {**copy.deepcopy(value), 'updated_at': now}
            elif action == 'space_delete':
                fields(value, ['id']); entity_id = identity(value['id'])
                if entity_id not in saved['spaces']:
                    raise ValueError('空间不存在')
                if any(p['space_id'] == entity_id for p in saved['pages'].values()):
                    raise ContentConflict('空间仍有页面（含回收区）；先将页面移出空间，页面不会被连带删除')
                del saved['spaces'][entity_id]
            elif action == 'project_save':
                fields(value, ['id', 'instructions', 'materials']); entity_id = identity(value['id'])
                if entity_id not in project_ids:
                    raise ValueError('项目不属于当前工作台')
                saved['projects'][entity_id] = copy.deepcopy(validate_project({k: v for k, v in value.items() if k != 'id'}))
            elif action == 'favorite':
                fields(value, ['id', 'favorite']); entity_id = identity(value['id'])
                if entity_id not in artifact_ids or type(value['favorite']) is not bool:
                    raise ValueError('只能收藏已登记作品')
                saved['favorites'][entity_id] = value['favorite']
            else:
                raise ValueError('未知内容操作')
            saved['revision'] += 1
            receipt = {'fingerprint': fingerprint, 'action': action, 'entity_id': entity_id, 'revision': saved['revision']}
            saved['receipts'][rid] = receipt
            validate_state(saved)
            encoded = json.dumps(saved, ensure_ascii=False).encode('utf-8')
            if len(encoded) > MAX_BYTES:
                raise ValueError('内容库已到容量上限；原数据保留')
            self._write(encoded)
            return {'state': {k: v for k, v in saved.items() if k != 'receipts'}, 'receipt': receipt, 'duplicate': False}

    def _references(self, value, project_ids, artifact_ids, state):
        if value['project_id'] and value['project_id'] not in project_ids:
            raise ValueError('页面项目不属于当前工作台')
        if value['space_id'] and value['space_id'] not in state['spaces']:
            raise ValueError('页面空间不存在')
        if not set(value['references']) <= set(artifact_ids):
            raise ValueError('页面只能引用已登记作品')

    def _write(self, encoded):
        self.guard()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix='.content-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                self.guard(); stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
            self.guard(); os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def export_page(page, version=None):
    number = page['version'] if version is None else version
    if type(number) is not int or not 1 <= number <= page['version']:
        raise ValueError('页面版本不存在')
    selected = page['versions'][number - 1]
    extension = {'text': '.txt', 'markdown': '.md', 'html': '.html'}[selected['format']]
    filename = re.sub(r'[^\w\-\u4e00-\u9fff ]', '_', selected['title'])[:100].strip() or 'page'
    return {'filename': filename + extension, 'mime_type': 'text/plain; charset=utf-8', 'text': selected['body'], 'version': number}


# Only an explicitly selected registered project is traversed. Hidden entries,
# common credential names, links and special files are not browser content.
_SECRET_NAMES = re.compile(r'(?:^|[._-])(?:credentials?|secrets?|tokens?|passwords?|private[_-]?key)(?:$|[._-])', re.I)


def browsable_name(name):
    return bool(name and not name.startswith('.') and not _SECRET_NAMES.search(name)
                and Path(name).suffix.lower() not in {'.pem', '.key', '.p12', '.pfx', '.keystore'}
                and name.lower() not in {'id_rsa', 'id_ed25519', 'id_ecdsa'})


def _relative_parts(relative):
    if not isinstance(relative, str) or len(relative) > 2048 or '\\' in relative or '\x00' in relative or relative.startswith('/'):
        raise ValueError('请选择项目内相对路径')
    if relative == '':
        return []
    parts = relative.split('/')
    if len(parts) > 32 or any(p in {'', '.', '..'} or not browsable_name(p) for p in parts):
        raise ValueError('此路径不在文件浏览范围内')
    return parts


def _open_project(root, relative, *, directory):
    import stat
    if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
        raise OSError('此平台不支持安全的项目文件浏览')
    path = Path(root)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('项目根目录无效')
    parts = _relative_parts(relative)
    if not directory and not parts:
        raise ValueError('请选择文件')
    handles = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(path.anchor, flags); handles.append(fd)
        for part in (*path.parts[1:], *(parts if directory else parts[:-1])):
            fd = os.open(part, flags, dir_fd=fd); handles.append(fd)
        if not directory:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd); handles.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('只支持独立普通文件；链接和特殊文件不可浏览')
        # Ownership of the final descriptor is transferred to the caller.
        handles.pop()
        return fd
    finally:
        for fd in reversed(handles):
            os.close(fd)


def project_files(root, relative=''):
    import stat
    fd = _open_project(root, relative, directory=True)
    entries, skipped, examined = [], 0, 0
    try:
        with os.scandir(fd) as iterator:
            for entry in iterator:
                examined += 1
                if examined > 5000:
                    break
                if not browsable_name(entry.name):
                    skipped += 1
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    directory = stat.S_ISDIR(info.st_mode)
                    regular = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                except OSError:
                    skipped += 1
                    continue
                if not directory and not regular:
                    skipped += 1
                    continue
                entries.append({'name': entry.name, 'path': '/'.join(filter(None, [relative, entry.name])),
                                'kind': 'directory' if directory else 'file', 'size': info.st_size if regular else None})
        entries.sort(key=lambda value: (value['kind'] != 'directory', value['name'].casefold()))
        truncated = len(entries) > 300 or examined > 5000
        return {'directory': relative, 'entries': entries[:300], 'truncated': truncated,
                'warning': '只列出项目内非隐藏普通文件和目录；凭据类名称、链接和特殊文件已排除。文件内容仍需由你核对，不会自动识别任意文本中的秘密。' + (' 当前目录列表已截断。' if truncated else ''), 'skipped': skipped}
    finally:
        os.close(fd)


def project_file(root, relative):
    from xiaoyu.artifact_views import is_text
    fd = _open_project(root, relative, directory=False)
    try:
        before = os.fstat(fd)
        raw = bytearray()
        while len(raw) < min(before.st_size, 65536):
            chunk = os.read(fd, min(65536 - len(raw), before.st_size - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(raw) != min(before.st_size, 65536):
            raise ValueError('读取时文件发生变化，请重新选择')
        available = is_text(raw)
        return {'path': relative, 'size': before.st_size, 'modified_at': before.st_mtime,
                'text': raw.decode('utf-8', errors='replace') if available else '', 'available': available,
                'truncated': before.st_size > len(raw), 'sha256': hashlib.sha256(raw).hexdigest() if before.st_size == len(raw) else '',
                'warning': '当前文件快照；引用后续修改不会自动更新任务草稿。' if available else '二进制文件只显示元数据，未提供文本引用。'}
    finally:
        os.close(fd)
