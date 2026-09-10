"""Persistent, scope-bound batches over the local collection queue and source-bound reports."""
import hashlib
import json
import threading
import uuid
from pathlib import Path

from .jobs import ACTIVE
from .reports import atomic_text, save_sources
from .store import InputError, now

MODES = ('research', 'breakdown', 'transcribe', 'download', 'comments', 'refresh')
TERMINAL = ('completed', 'partial', 'cancelled', 'failed')


def recommend(posts, count=6):
    """Transparent, deduplicated sample selection, not a proprietary opportunity score."""
    recent = sorted(posts, key=lambda p: (p.get('published_at', ''), p['aweme_id']), reverse=True)
    ranked = [('近期发布', recent, 2), ('本批点赞靠前', sorted(posts, key=lambda p: p.get('likes') or 0, reverse=True), 2),
              ('本批评论靠前', sorted(posts, key=lambda p: p.get('comments') or 0, reverse=True), 1),
              ('本批收藏靠前', sorted(posts, key=lambda p: p.get('collects') or 0, reverse=True), 1),
              ('补充近期样本', recent, count)]
    chosen = {}
    for reason, rows, quota in ranked:
        added = 0
        for p in rows:
            if len(chosen) >= count or added >= quota:
                break
            if p['aweme_id'] in chosen:
                continue
            chosen[p['aweme_id']] = reason
            added += 1
    return chosen


class Research:
    def __init__(self, store, jobs, base_url, start=True):
        self.store, self.jobs, self.base_url = store, jobs, base_url
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.last_error = ''
        for profile in store.profiles():
            scope = profile['service_account_key']
            for legacy in store.records(scope, 'research_batch'):
                changed = False
                if legacy['status'] == 'awaiting_codex':
                    legacy['status'] = 'awaiting_ai'; changed = True
                for account in legacy['accounts']:
                    if account['status'] == 'awaiting_codex':
                        account['status'] = 'awaiting_ai'; changed = True
                if changed:
                    legacy.update(ai_auto=False, message='已升级为 API 研究，请在设置中配置接口后继续。')
                    self.save(legacy)
            for legacy in store.records(scope, 'report'):
                if legacy['status'] == 'awaiting_codex':
                    store.merge(scope, 'report', legacy['id'], {'status': 'awaiting_ai', 'phase': 'waiting', 'progress_message': '请配置 AI 接口后继续研究。'})
            for batch in store.records(scope, 'research_batch'):
                if batch['status'] in ('scanning', 'preparing', 'analyzing', 'cancelling'):
                    store.merge(scope, 'research_batch', batch['id'], {'status': 'interrupted', 'ai_auto': False,
                        'message': '服务已重启，点击恢复；已完成的文件和研究均保留。'})
        self.thread = threading.Thread(target=self.run, daemon=True, name='linsi-research-coordinator')
        if start:
            self.thread.start()

    def save(self, batch):
        try:
            previous = self.store.get(batch['service_account_key'], 'research_batch', batch['id'])
            if {k: v for k, v in previous.items() if k != 'updated_at'} == {k: v for k, v in batch.items() if k != 'updated_at'}:
                return previous
        except InputError:
            pass
        return self.store.merge(batch['service_account_key'], 'research_batch', batch['id'], batch)

    def rows(self, scope, account, count, ids=None):
        posts = [p for p in self.store.records(scope, 'post') if p['sec_uid'] == account['sec_uid']]
        chosen = {i: '手动选择' for i in ids} if ids is not None else recommend(posts, count)
        # Default review shows a bounded recent sample and every recommended item.
        recent = sorted(posts, key=lambda p: (p.get('published_at', ''), p['aweme_id']), reverse=True)
        pool = [p for p in recent if p['aweme_id'] in chosen] + [p for p in recent if p['aweme_id'] not in chosen]
        if ids is not None:
            pool = [p for p in pool if p['aweme_id'] in chosen]
        return [{'aweme_id': p['aweme_id'], 'title': p['title'], 'selected': p['aweme_id'] in chosen,
                 'reason': chosen.get(p['aweme_id'], '可手动加入'), 'mode': 'breakdown'} for p in pool[:30]]

    def create(self, scope, body):
        profile = self.store.profile(scope)
        if profile['demo']:
            raise InputError('请在真实工作台进行批量研究；演示资料不能连接抖音。')
        mode = body.get('mode', 'research')
        if mode not in MODES:
            raise InputError('批量处理方式不正确')
        count = body.get('sample_count', 6)
        if type(count) is not int or not 1 <= count <= 30:
            raise InputError('每个账号请选择 1～30 条代表作品')
        ids = body.get('aweme_ids')
        sec_uids = body.get('sec_uids', [])
        if ids is not None:
            if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
                raise InputError('请选择 1～100 条不重复的作品，每个账号最多 30 条')
            posts = [self.store.get(scope, 'post', i) for i in ids]
            sec_uids = list(dict.fromkeys(p['sec_uid'] for p in posts))
            if any(sum(p['sec_uid'] == s for p in posts) > 30 for s in sec_uids):
                raise InputError('每个账号最多处理 30 条作品，请分批选择')
        if not isinstance(sec_uids, list) or not 1 <= len(sec_uids) <= 10 or any(not isinstance(s, str) for s in sec_uids) or len(set(sec_uids)) != len(sec_uids):
            raise InputError('请选择 1～10 个不重复的账号；首次建议 3 个以内')
        accounts = [self.store.get(scope, 'account', s) for s in sec_uids]
        fingerprint = hashlib.sha256(json.dumps([mode, sorted(sec_uids), sorted(ids or []), count,
                                                 body.get('refresh') is True], sort_keys=True).encode()).hexdigest()
        with self.lock:
            existing = next((b for b in self.store.records(scope, 'research_batch') if b.get('fingerprint') == fingerprint and b['status'] not in TERMINAL), None)
            if existing:
                return self.get(scope, existing['id'])
            batch = {'id': uuid.uuid4().hex, 'service_account_key': scope, 'mode': mode, 'sample_count': count,
                     'status': 'review_ready', 'created_at': now(), 'fingerprint': fingerprint, 'accounts': [],
                     'comments': False, 'comment_limit': 30, 'ai_auto': False, 'allow_partial': False,
                     'message': '确认作品清单后开始处理。'}
            for a in accounts:
                selected_ids = [p['aweme_id'] for p in posts if p['sec_uid'] == a['sec_uid']] if ids is not None else None
                items = self.rows(scope, a, count, selected_ids)
                scan = (not items or body.get('refresh') is True) and ids is None
                batch['accounts'].append({'sec_uid': a['sec_uid'], 'name': a['name'], 'status': 'scanning' if scan else 'review_ready',
                    'items': items, 'jobs': {}, 'report_ids': {}, 'scan_required': scan, 'allow_partial': False})
                if scan:
                    batch['status'] = 'scanning'
            self.save(batch)
            return self.get(scope, batch['id'])

    def confirm(self, scope, identity, body):
        with self.lock:
            batch = self.store.get(scope, 'research_batch', identity)
            if batch['status'] not in ('review_ready', 'scan_failed'):
                if batch.get('confirmed_at'):
                    return self.get(scope, identity)  # double-click is idempotent
                raise InputError('请等待账号读取完成')
            incoming = body.get('accounts')
            if not isinstance(incoming, list) or len(incoming) != len(batch['accounts']):
                raise InputError('请提交完整账号选择清单')
            mapping = {a.get('sec_uid'): a for a in incoming if isinstance(a, dict)}
            if len(mapping) != len(incoming) or set(mapping) != {a['sec_uid'] for a in batch['accounts']}:
                raise InputError('账号选择与当前批次不一致')
            total = 0
            for a in batch['accounts']:
                choices = mapping[a['sec_uid']].get('items', [])
                if not isinstance(choices, list) or len(choices) > 30:
                    raise InputError('每个账号最多 30 条作品')
                keys = [i.get('aweme_id') for i in choices if isinstance(i, dict)]
                known = {i['aweme_id']: i for i in a['items']}
                if len(keys) != len(choices) or any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys) or any(k not in known for k in keys):
                    raise InputError('所选作品不属于本账号的研究清单')
                for item in a['items']:
                    item['selected'] = item['aweme_id'] in keys
                for item in choices:
                    p = self.store.get(scope, 'post', item['aweme_id'])
                    if p['sec_uid'] != a['sec_uid'] or item.get('mode', 'breakdown') not in ('transcribe', 'breakdown'):
                        raise InputError('作品账号或处理方式不正确')
                    known[item['aweme_id']]['mode'] = item.get('mode', 'breakdown')
                    reason = item.get('reason', '手动选择')
                    known[item['aweme_id']]['reason'] = reason if reason in ('手动选择', '近期发布', '本批点赞靠前', '本批评论靠前', '本批收藏靠前', '补充近期样本') else '手动选择'
                total += len(keys)
                a['status'] = 'preparing' if keys else 'skipped'
                a.pop('error', None)
            if not total and batch['mode'] != 'refresh':
                raise InputError('请至少选择一条作品')
            if total > 100:
                raise InputError('本批超过 100 条，请减少账号或每个账号的样本')
            limit = body.get('comment_limit', 30)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise InputError('每条作品采集 1～100 条评论')
            batch.update(status='preparing', comments=body.get('comments') is True, comment_limit=limit,
                         confirmed_at=now(), message='按确认清单处理，已有成果自动复用。')
            if batch['mode'] == 'refresh':
                for a in batch['accounts']:
                    a['status'] = 'preparing';a['jobs'].pop('refresh', None)
            self.save(batch)
            return self.get(scope, identity)

    def child(self, batch, account, action, payload, key=None):
        key = key or action
        scope = batch['service_account_key']
        if key in account['jobs']:
            return self.store.get(scope, 'job', account['jobs'][key])
        # Keep room for a manual login/verification task. Queue pressure is not a failure.
        if sum(j['status'] in ACTIVE for j in self.store.records(scope, 'job')) >= 4:
            return None
        try:
            job = self.jobs.submit(scope, action, payload)
        except InputError as e:
            if '同样的任务' in str(e):
                # Shared identical work can be observed, but must not be cancelled by this batch.
                job = next(j for j in self.store.records(scope, 'job') if j['status'] in ACTIVE and j['action'] == action and j['payload'] == payload)
            else:
                raise
        else:
            self.store.merge(scope, 'job', job['id'], {'batch_id': batch['id']})
        account['jobs'][key] = job['id']
        self.save(batch)
        return job

    def scan(self, batch, account):
        scope = batch['service_account_key']
        original = self.store.get(scope, 'account', account['sec_uid'])
        j = self.child(batch, account, 'collect', {'link': original['url'], 'more': False}, 'scan')
        if not j or j['status'] in ACTIVE:
            return
        if j['status'] != 'completed' or j.get('result', {}).get('sec_uid') != account['sec_uid']:
            account.update(status='scan_failed', error=j.get('message', '账号读取失败，请检查链接和抖音窗口'))
        else:
            account['items'] = self.rows(scope, original, batch['sample_count'])
            account['status'] = 'review_ready'
        self.save(batch)

    def media_exists(self, post):
        path = (self.store.directory / post.get('media_path', 'missing')).resolve()
        return path.is_relative_to(self.store.directory / 'media') and path.is_file() and path.stat().st_size > 0

    def comments_ready(self, scope, post, limit):
        return post.get('comments_status') == 'completed' and (
            post.get('comments_has_more') is False or
            sum(c['aweme_id'] == post['aweme_id'] for c in self.store.records(scope, 'comment')) >= limit)

    def prepare(self, batch, account):
        scope, mode = batch['service_account_key'], batch['mode']
        selected = [i for i in account['items'] if i['selected']]
        posts = [self.store.get(scope, 'post', i['aweme_id']) for i in selected]
        if mode == 'refresh':
            a = self.store.get(scope, 'account', account['sec_uid'])
            j = self.child(batch, account, 'collect', {'link': a['url'], 'more': False}, 'refresh')
            if j and j['status'] not in ACTIVE:
                account.update(status='completed' if j['status'] == 'completed' and j.get('result', {}).get('sec_uid') == a['sec_uid'] else 'partial_ready', error='' if j['status'] == 'completed' else j['message'])
                self.save(batch)
            return
        media_action = 'download' if mode == 'download' else 'transcribe'
        needed = [p['aweme_id'] for p in posts if p.get('media_type') != 'gallery' and
                  (not self.media_exists(p) if mode == 'download' else not p.get('transcript', '').strip())]
        if mode != 'comments' and needed or media_action in account['jobs']:
            j = self.child(batch, account, media_action, {'aweme_ids': needed})
            if not j or j['status'] in ACTIVE:
                return
        if batch['comments'] or mode == 'comments':
            needed_comments = [p['aweme_id'] for p in posts if not self.comments_ready(scope, p, batch['comment_limit'])]
            if needed_comments or 'comments' in account['jobs']:
                j = self.child(batch, account, 'comments', {'aweme_ids': needed_comments, 'limit': batch['comment_limit']})
                if not j or j['status'] in ACTIVE:
                    return
        # Re-read after child workers; success always depends on saved result, never on title.
        ready, failed, skipped, warnings = [], [], [], []
        for item in selected:
            p = self.store.get(scope, 'post', item['aweme_id'])
            is_gallery = p.get('media_type') == 'gallery'
            ok = p.get('comments_status') == 'completed' if mode == 'comments' else self.media_exists(p) if mode == 'download' else bool(p.get('transcript', '').strip())
            if is_gallery and mode in ('download', 'transcribe'):
                skipped.append(item['aweme_id']);item['message'] = '图文没有视频语音，已跳过；可查看图片、补充正文和采集评论。'
            elif ok:
                ready.append(item['aweme_id']);item['message'] = '结果已保存，可直接查看。'
            else:
                failed.append(item['aweme_id']);item['message'] = ('图文缺少正文，请补充可读文字后继续研究。' if is_gallery else p.get('comments_error') if mode == 'comments' else p.get('processing_error')) or '未取得有效结果，请检查后重试。'
            item['status'] = 'skipped' if item['aweme_id'] in skipped else 'ready' if ok else 'failed'
            item['comment_warning'] = (p.get('comments_error') or '未取得本次请求的评论样本，可重试。') if batch['comments'] and not self.comments_ready(scope, p, batch['comment_limit']) else ''
            if item['comment_warning']:
                warnings.append(item['aweme_id'])
        account.update(ready_ids=ready, failed_ids=failed, skipped_ids=skipped, comment_failed_ids=warnings)
        if failed and not account.get('allow_partial'):
            account['status'] = 'partial_ready'
        elif mode in ('download', 'transcribe', 'comments'):
            account['status'] = 'partial' if failed or warnings else 'completed'
        elif ready:
            account['status'] = 'awaiting_ai'
            self.make_reports(batch, account)
        else:
            account['status'] = 'partial_ready'
        self.save(batch)

    def create_report(self, batch, account, ids, kind, key):
        scope = batch['service_account_key']
        rid = uuid.uuid5(uuid.NAMESPACE_URL, batch['id'] + account['sec_uid'] + key).hex
        try:
            report = self.store.get(scope, 'report', rid)
        except InputError:
            report = self.store.create_report(scope, {'sec_uid': account['sec_uid'], 'aweme_ids': ids, 'report_type': kind}, identity=rid)
            # Add only comments from these exact sources to an immutable research snapshot.
            comments = [c for c in self.store.records(scope, 'comment') if c['aweme_id'] in ids]
            comments.sort(key=lambda c: c.get('likes') or 0, reverse=True)
            snapshot = {**report['snapshot'], 'sample_comments': comments[:300], 'available_comment_count': len(comments), 'batch_id': batch['id']}
            if kind == 'account':
                snapshot['completed_work_analyses'] = [
                    {'aweme_id': aweme, 'report_id': existing['id'], 'markdown': existing['markdown']}
                    for aweme, report_id in account['report_ids'].items() if aweme in ids
                    for existing in [self.store.get(scope, 'report', report_id)] if existing['status'] == 'completed']
            digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            report = self.store.merge(scope, 'report', rid, {'snapshot': snapshot, 'source_digest': digest, 'batch_id': batch['id']})
        save_sources(self.store.directory, report)
        return rid

    def make_reports(self, batch, account):
        for item in account['items']:
            if not item['selected'] or item['aweme_id'] not in account.get('ready_ids', []) or item['mode'] != 'breakdown':
                continue
            if item['aweme_id'] not in account['report_ids']:
                account['report_ids'][item['aweme_id']] = self.create_report(batch, account, [item['aweme_id']], 'breakdown', item['aweme_id'])
                self.save(batch)
        self.advance_reports(batch, account)

    def advance_reports(self, batch, account):
        scope = batch['service_account_key']
        reports = [self.store.get(scope, 'report', rid) for rid in account['report_ids'].values()]
        if any(r['status'] != 'completed' for r in reports):
            account['status'] = 'analyzing' if any(r['status'] == 'analyzing' for r in reports) else 'awaiting_ai'
            return
        if batch['mode'] == 'research' and account.get('ready_ids'):
            source_key = hashlib.sha256(json.dumps(sorted(account['ready_ids'])).encode()).hexdigest()[:16]
            if account.get('summary_source_key') != source_key:
                account['summary_id'] = self.create_report(batch, account, account['ready_ids'], 'account', 'summary-' + source_key)
                account['summary_source_key'] = source_key
                self.save(batch)
        if account.get('summary_id'):
            summary = self.store.get(scope, 'report', account['summary_id'])
            if summary['status'] != 'completed':
                account['status'] = 'analyzing' if summary['status'] == 'analyzing' else 'awaiting_ai'
                return
        account['status'] = 'partial' if account.get('failed_ids') or account.get('comment_failed_ids') else 'completed'

    def tick(self, batch):
        if batch['status'] in TERMINAL or batch['status'] in ('interrupted', 'review_ready', 'scan_failed'):
            return
        for account in batch['accounts']:
            try:
                if account['status'] == 'scanning':
                    self.scan(batch, account)
                elif account['status'] == 'preparing':
                    self.prepare(batch, account)
                elif account['status'] in ('awaiting_ai', 'analyzing'):
                    self.advance_reports(batch, account)
            except InputError as error:
                account.update(status='partial_ready', error=str(error))
        states = [a['status'] for a in batch['accounts']]
        if 'scanning' in states:
            batch.update(status='scanning', message='正在读取账号的可访问作品…')
        elif not batch.get('confirmed_at'):
            batch.update(status='scan_failed' if 'scan_failed' in states else 'review_ready', message='请确认本次作品清单，未读取的账号可以重试。')
        elif 'preparing' in states:
            batch.update(status='preparing', message='本机正在处理已确认作品；关闭页面不会取消任务。')
        elif 'analyzing' in states:
            batch.update(status='analyzing', message='AI 接口正在分析已选来源。')
        elif 'awaiting_ai' in states:
            batch.update(status='awaiting_ai', message='资料已就绪，已启用的研究按顺序执行。' if batch.get('ai_auto') else '研究已暂停，请重试当前报告，或恢复整批研究。' if batch.get('ai_error') else '资料已就绪，可开始研究。')
        elif 'partial_ready' in states:
            batch.update(status='partial_ready', message='部分作品未取得结果，可重试失败项或先使用成功作品。')
        else:
            batch.update(status='partial' if 'partial' in states else 'completed', message='处理结束，按账号查看已保存结果。')
        self.save(batch)

    def resume(self, scope, identity, partial=False):
        with self.lock:
            b = self.store.get(scope, 'research_batch', identity)
            if b['status'] in ('scanning', 'preparing', 'analyzing', 'cancelling'):
                raise InputError('任务仍在运行，请等待当前步骤完成')
            if b['status'] in ('cancelled', 'interrupted') and any(
                    self.store.get(scope, 'job', rid)['status'] in ACTIVE for a in b['accounts'] for rid in a['jobs'].values()):
                raise InputError('上一轮任务仍在结束，请稍后恢复；已保存结果保留。')
            for a in b['accounts']:
                if a['status'] in ('completed', 'skipped'):
                    continue
                if a['status'] in ('awaiting_ai', 'analyzing'):
                    continue
                if not b.get('confirmed_at'):
                    a['status'] = 'scanning' if a.get('scan_required') else 'review_ready'
                    a['jobs'].pop('scan', None)
                else:
                    a['allow_partial'] = partial
                    a['status'] = 'preparing'
                    if not partial:
                        if a.get('comment_failed_ids'):
                            a['jobs'].pop('comments', None)
                        for key, rid in list(a['jobs'].items()):
                            if self.store.get(scope, 'job', rid)['status'] not in ('completed', *ACTIVE):
                                del a['jobs'][key]
                a.pop('error', None)
            b['status'] = 'preparing' if b.get('confirmed_at') else 'scanning'
            self.save(b)
            return self.get(scope, identity)

    def cancel(self, scope, identity):
        with self.lock:
            b = self.store.get(scope, 'research_batch', identity)
            for a in b['accounts']:
                for rid in a['jobs'].values():
                    job = self.store.get(scope, 'job', rid)
                    if job.get('batch_id') == identity and job['status'] in ACTIVE:
                        self.jobs.cancel(scope, rid)
                for rid in [*a['report_ids'].values(), *([a['summary_id']] if a.get('summary_id') else [])]:
                    r = self.store.get(scope, 'report', rid)
                    if r['status'] != 'completed':
                        self.store.merge(scope, 'report', rid, {'status': 'paused', 'phase': 'failed', 'api_requested': False,
                            'api_error': '本批研究已停止，可恢复后继续。', 'updated_at': now()})
            b.update(status='cancelled', ai_auto=False, message='已停止后续调度，当前步骤正在结束；已保存资料保留。')
            self.save(b)
            return self.get(scope, identity)

    def get(self, scope, identity):
        with self.lock:
            b = self.store.get(scope, 'research_batch', identity)
            posts = {p['aweme_id']: p for p in self.store.records(scope, 'post')}
            for a in b['accounts']:
                original = self.store.get(scope, 'account', a['sec_uid'])
                a['read_count'] = sum(p['sec_uid'] == a['sec_uid'] for p in posts.values())
                a['reported_count'] = original.get('work_count')
                a['active_jobs'] = [self.store.get(scope, 'job', rid) for rid in a['jobs'].values() if self.store.get(scope, 'job', rid)['status'] in ACTIVE]
                for item in a['items']:
                    p = posts.get(item['aweme_id'], {})
                    item.update({k: p.get(k) for k in ('media_type', 'image_path', 'likes', 'comments', 'collects', 'shares', 'published_at')})
                    item['text_ready'] = bool(p.get('transcript', '').strip())
                    item['media_ready'] = self.media_exists(p)
                    rid = a['report_ids'].get(item['aweme_id'])
                    if rid:
                        r = self.store.get(scope, 'report', rid)
                        item.update(report_id=rid, report_status=r['status'], report_phase=r.get('phase', 'waiting'))
                if a.get('summary_id'):
                    r = self.store.get(scope, 'report', a['summary_id'])
                    a['summary_status'] = r['status']
            return b

    def summaries(self, scope):
        self.store.profile(scope)
        return [{k: b.get(k) for k in ('id', 'mode', 'status', 'message', 'created_at', 'updated_at', 'ai_auto', 'ai_error')} |
                {'account_count': len(b['accounts']), 'selected_count': sum(i['selected'] for a in b['accounts'] for i in a['items'])}
                for b in self.store.records(scope, 'research_batch')]

    def packet(self, scope, identity):
        b = self.get(scope, identity)
        tasks = []
        for a in b['accounts']:
            for rid in [*a['report_ids'].values(), *([a['summary_id']] if a.get('summary_id') else [])]:
                r = self.store.get(scope, 'report', rid)
                if r['status'] != 'completed':
                    tasks.append({'report_id': rid, 'service_account_key': scope, 'source_digest': r['source_digest'], 'report_type': r['report_type'], **r['snapshot']})
        return {'batch_id': identity, 'service_account_key': scope, 'status': b['status'], 'tasks': tasks,
                'instruction': '先完成逐条拆解，再重新获取本接口；账号汇总任务在逐条完成后生成。仅按来源分析，回写已有报告，不创建脚本。'}

    def export(self, scope, identity):
        b = self.get(scope, identity)
        lines = ['# 灵思 · 批量研究成果', '', f"批次：{identity}", f"状态：{b['status']}", '']
        for a in b['accounts']:
            lines += [f"## {a['name']}", f"账号：{a['sec_uid']}", '']
            for rid in [*([a['summary_id']] if a.get('summary_id') else []), *a['report_ids'].values()]:
                report = self.store.get(scope, 'report', rid)
                if report['status'] == 'completed':
                    lines += [report['markdown'], '']
            for i in a['items']:
                if i['selected']:
                    p = self.store.get(scope, 'post', i['aweme_id'])
                    lines += [f"### 来源：{p['title']}", f"aweme_id：{p['aweme_id']}", p.get('transcript') or '暂无有效全文', '']
        return '\n'.join(lines)

    def run(self):
        while not self.stop.wait(.5):
            try:
                with self.lock:
                    for profile in self.store.profiles():
                        for batch in self.store.records(profile['service_account_key'], 'research_batch'):
                            self.tick(batch)
            except Exception as error:
                self.last_error = type(error).__name__  # no source text or credentials in logs

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
