"""同分母汇总、配对比较及独立成本口径；不回写历史报告。"""
from collections import Counter
from itertools import combinations
import statistics


def valid_score(row):
    return (row.get('status') == 'complete' and row.get('grade', {}).get('status') == 'complete'
            and not row['grade'].get('artifact_only', False))


def execution_complete(run):
    return len(run['rounds']) == 3 and all(r['status'] == 'complete' for r in run['rounds'])


def final_row(run):
    return run['rounds'][-1] if len(run['rounds']) == 3 else {}


def failure_kind(row):
    if row['status'] == 'complete':
        return 'judge_error' if row.get('grade', {}).get('status') == 'error' else None
    if row['status'] == 'skipped_after_incomplete_turn':
        return 'skipped_after_execution_failure'
    if row.get('error_type') == 'BudgetError':
        return 'context_capacity'
    if row['status'] == 'memory_failed':
        return 'memory_maintenance'
    if row['status'] == 'cost_limit':
        return 'cost_limit'
    if row['status'] == 'timeout':
        return 'timeout'
    if row.get('error_kind'):
        return row['error_kind']
    return 'execution_error'


def summarize_runs(runs, planned=None):
    groups = {}
    names = sorted({r['group'] for r in runs} | {r['group'] for r in planned or []})
    for group in names:
        subset = [r for r in runs if r['group'] == group]
        scheduled = [r for r in planned or [] if r['group'] == group]
        expected = len(scheduled) if planned is not None else len(subset)
        result = dict(task_runs=len(subset), planned_runs=expected, not_started_runs=expected - len(subset),
                      complete_runs=0, failed_runs=0, execution_complete_runs=0, execution_failed_runs=0,
                      judge_failed_runs=0, unscored_execution_complete_runs=0,
                      first_mean_score=None, final_mean_score=None,
                      final_pass_rate=0, planned_final_pass_rate=0, earliest_pass_rounds=[], tasks={})
        first, final, passes = [], [], 0
        for run in subset:
            rounds = run['rounds']
            executed = execution_complete(run)
            complete = executed and all(valid_score(r) for r in rounds)
            result['execution_complete_runs'] += executed
            result['execution_failed_runs'] += not executed
            result['judge_failed_runs'] += any(r.get('grade', {}).get('status') == 'error' for r in rounds)
            result['unscored_execution_complete_runs'] += executed and not complete
            result['complete_runs'] += complete
            result['failed_runs'] += not complete
            earliest = next((r['round'] for r in rounds if valid_score(r) and r['grade']['passed']), None)
            result['earliest_pass_rounds'].append(dict(task_id=run['task_id'], repeat=run['repeat'], round=earliest))
            for bucket, row in ((first, rounds[0] if rounds else {}), (final, final_row(run))):
                if valid_score(row):
                    bucket.append(row['grade']['score'])
            passed = complete and final_row(run)['grade']['passed']
            passes += passed
            task = result['tasks'].setdefault(run['task_id'], dict(runs=0, final_passes=0, final_dimensions={}))
            task['runs'] += 1
            task['final_passes'] += passed
            if valid_score(final_row(run)):
                for dim, values in final_row(run)['grade']['dimensions'].items():
                    totals = task['final_dimensions'].setdefault(dim, dict(score=0, max_score=0))
                    for key in totals:
                        totals[key] += values[key]
        result['first_mean_score'] = statistics.mean(first) if first else None
        result['final_mean_score'] = statistics.mean(final) if final else None
        result['scored_first_runs'], result['scored_final_runs'] = len(first), len(final)
        result['final_pass_rate'] = passes / len(subset) if subset else None
        result['planned_final_pass_rate'] = passes / expected if expected else None
        result['failure_rate'] = result['failed_runs'] / len(subset) if subset else None
        rounds = [r for run in subset for r in run['rounds']]
        result['failure_causes'] = dict(Counter(filter(None, (failure_kind(r) for r in rounds))))
        result['timeout_rounds'] = sum(r['status'] == 'timeout' for r in rounds)
        result['judge_error_rounds'] = sum(r.get('grade', {}).get('status') == 'error' for r in rounds)
        result['budget_closeout_rounds'] = sum(r.get('completion_reason') == 'budget_closeout' for r in rounds)
        result['mean_elapsed_seconds'] = (statistics.mean(r.get('elapsed_seconds', 0) for r in subset)
                                          if subset else None)
        groups[group] = result
    return groups


def paired_comparisons(runs, planned, costs=None):
    """仅对相同 task/repeat 的有效轮次配对；缺失公开，绝不补零或挑不同题比较。"""
    by_key = {(r['task_id'], r['repeat'], r['group']): r for r in runs}
    names = list(dict.fromkeys(p['group'] for p in planned))
    keys = list(dict.fromkeys((p['task_id'], p['repeat']) for p in planned))
    categories = {p['task_id']: p['category'] for p in planned}
    run_costs = {(r['task_id'], r['repeat'], r['group']): r for r in (costs or {}).get('by_run', [])}
    comparisons = []
    for left, right in combinations(names, 2):
        for stage in ('first', 'final'):
            for category in ('all', *sorted(set(categories.values()))):
                rows, excluded = [], []
                for task, repeat in keys:
                    if category != 'all' and categories[task] != category:
                        continue
                    a, b = by_key.get((task, repeat, left)), by_key.get((task, repeat, right))
                    ar = (a['rounds'][0] if stage == 'first' and a['rounds'] else final_row(a)) if a else {}
                    br = (b['rounds'][0] if stage == 'first' and b['rounds'] else final_row(b)) if b else {}
                    if not valid_score(ar) or not valid_score(br):
                        excluded.append(dict(task_id=task, repeat=repeat,
                                             left_status=ar.get('status', 'not_available'),
                                             right_status=br.get('status', 'not_available'),
                                             left_grade=ar.get('grade', {}).get('status'),
                                             right_grade=br.get('grade', {}).get('status')))
                        continue
                    dimensions = {}
                    for dim in ('memory', 'mixed', 'general'):
                        av, bv = ar['grade']['dimensions'].get(dim), br['grade']['dimensions'].get(dim)
                        if av and bv and av['max_score']:
                            dimensions[dim] = dict(left=av['score'], right=bv['score'], max_score=av['max_score'])
                    row = dict(task_id=task, repeat=repeat, left_score=ar['grade']['score'],
                               right_score=br['grade']['score'],
                               delta=ar['grade']['score'] - br['grade']['score'], dimensions=dimensions)
                    if stage == 'final' and all((task, repeat, g) in run_costs for g in (left, right)):
                        row['workload'] = {side: run_costs[(task, repeat, group)]
                                           for side, group in [('left', left), ('right', right)]}
                    rows.append(row)
                deltas = [r['delta'] for r in rows]
                cost_rows = [r['workload'] for r in rows if 'workload' in r]
                comparisons.append(dict(left=left, right=right, stage=stage, category=category,
                                        planned_pairs=len(rows) + len(excluded), valid_pairs=len(rows),
                                        left_mean=statistics.mean(r['left_score'] for r in rows) if rows else None,
                                        right_mean=statistics.mean(r['right_score'] for r in rows) if rows else None,
                                        mean_delta=statistics.mean(deltas) if rows else None,
                                        delta_stddev=statistics.stdev(deltas) if len(deltas) > 1 else None,
                                        distinct_tasks=len({r['task_id'] for r in rows}),
                                        cost_pairs=len(cost_rows),
                                        left_mean_online_cost=statistics.mean(r['left']['online_cost'] for r in cost_rows) if cost_rows else None,
                                        right_mean_online_cost=statistics.mean(r['right']['online_cost'] for r in cost_rows) if cost_rows else None,
                                        rows=rows, excluded=excluded))
    return comparisons


def cost_breakdown(events, planned, runs=(), *, simulated=False, reused_seed=False):
    names = list(dict.fromkeys(p['group'] for p in planned))
    groups = {}
    for group in names:
        totals = {stage: dict(calls=0, cost=0, input_tokens=0, output_tokens=0, estimated_calls=0)
                  for stage in ('build', 'online', 'judge')}
        for event in events:
            if event.get('group') != group or event.get('stage') not in totals:
                continue
            total = totals[event['stage']]
            total['calls'] += 1
            total['cost'] += event['cost']
            total['input_tokens'] += sum(event['tokens'].get(k, 0) for k in ('input', 'cache_read', 'cache_write'))
            total['output_tokens'] += event['tokens'].get('output', 0)
            total['estimated_calls'] += event['usage_mode'] != 'provider_usage'
        groups[group] = dict(stages=totals,
                             workload_cost=totals['online']['cost'],
                             build_plus_workload_cost=totals['build']['cost'] + totals['online']['cost'],
                             seed_cost_included=not reused_seed)
    by_run = []
    indexed = {(r['task_id'], r['repeat'], r['group']): r for r in runs}
    for item in planned:
        key = (item['task_id'], item['repeat'], item['group'])
        rows = [e for e in events if e.get('stage') == 'online' and
                (e.get('task_id'), e.get('repeat'), e.get('group')) == key]
        run = indexed.get(key)
        by_run.append({**item, 'started': run is not None,
                       'execution_complete': execution_complete(run) if run else False,
                       'online_cost': sum(e['cost'] for e in rows),
                       'online_input_tokens': sum(sum(e['tokens'].get(k, 0) for k in
                                                     ('input', 'cache_read', 'cache_write')) for e in rows),
                       'online_output_tokens': sum(e['tokens'].get('output', 0) for e in rows),
                       'online_calls': len(rows),
                       'estimated_calls': sum(e['usage_mode'] != 'provider_usage' for e in rows)})
    return dict(simulated=simulated, groups=groups, by_run=by_run,
                note='judge 为评测开销，独立列出；复用快照的建库历史成本不在本次账本内。'
                     '不同完成样本/轮次数的总成本不可直接解释为效率收益。')
