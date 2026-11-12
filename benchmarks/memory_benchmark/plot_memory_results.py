"""从已有报告生成六个记忆任务的独立分析与静态图，不请求模型、不修改原证据。"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import statistics

from .reporting import execution_complete, valid_score

GROUPS = ('none', 'legacy', 'full_history', 'lamemory')
LABELS = ('无长期记忆', 'legacy', '完整历史', 'LaMemory')
TASKS = {
    '01_parents_birthday': '父母生日',
    '02_therapy_schedule': '理疗安排',
    '03_sql_study_blocks': 'SQL 学习',
    '04_project_risk_kanban': '项目风险',
    '05_quarter_learning_plan': '季度计划',
    '06_finance_review': '财务复盘',
}
COLORS = ('#aeb8c7', '#7085a2', '#b99b76', '#00836d')
INK = '#203247'


def usage(rows):
    def inp(row):
        return sum(row['tokens'].get(k, 0) for k in ('input', 'cache_read', 'cache_write'))
    return dict(input_tokens=sum(inp(r) for r in rows),
                output_tokens=sum(r['tokens'].get('output', 0) for r in rows),
                total_tokens=sum(inp(r) + r['tokens'].get('output', 0) for r in rows),
                cost=sum(r['cost'] for r in rows), calls=len(rows),
                agent_calls=sum(r['role'] == 'agent' for r in rows),
                agent_input=sum(inp(r) for r in rows if r['role'] == 'agent'),
                agent_output=sum(r['tokens'].get('output', 0) for r in rows if r['role'] == 'agent'),
                maintenance_tokens=sum(inp(r) + r['tokens'].get('output', 0) for r in rows if r['role'] != 'agent'),
                maintenance_cost=sum(r['cost'] for r in rows if r['role'] != 'agent'))


def aggregate(run):
    report_path, usage_path = run / 'report.json', run / 'usage.jsonl'
    report = json.loads(report_path.read_text())
    events = [json.loads(line) for line in usage_path.read_text().splitlines()]
    if tuple(report['settings']['groups']) != GROUPS or report['settings']['repeats'] != 1:
        raise ValueError('此分析协议要求四组且每题仅一次独立重复，不能静默混用分母')
    runs = {(r['task_id'], r['group']): r for r in report['runs'] if r['task_id'] in TASKS}
    if len(runs) != len(TASKS) * len(GROUPS):
        raise ValueError('六个记忆任务必须全部启动，不能遗漏样本')
    complete = lambda r: execution_complete(r) and all(valid_score(row) for row in r['rounds'])
    common = [task for task in TASKS if all(complete(runs[task, g]) for g in GROUPS)]
    if not common:
        raise ValueError('没有四组共同完成的任务，不能比较资源总量')
    groups, scores = {}, []
    for group in GROUPS:
        selected = [runs[t, group] for t in TASKS]
        valid = [r['rounds'][-1]['grade']['score'] for r in selected if complete(r)]
        passed = sum(complete(r) and r['rounds'][-1]['grade']['passed'] for r in selected)
        first_contexts = []
        for task in TASKS:
            r = runs[task, group]
            final = r['rounds'][-1] if len(r['rounds']) == 3 else {}
            score = final['grade']['score'] if complete(r) else None
            scores.append(dict(task_id=task, task=TASKS[task], group=group,
                               completed=complete(r), passed=complete(r) and final['grade']['passed'],
                               score=score, online=usage([e for e in events if e['group'] == group and
                                                        e['stage'] == 'online' and e.get('task_id') == task]),
                               rounds=[dict(round=row['round'], status=row['status'],
                                                        score=row.get('grade', {}).get('score') if valid_score(row) else None,
                                                        error_kind=row.get('error_kind'), error=row.get('error'))
                                                    for row in r['rounds']]))
            trace = run / 'runs' / task / f'repeat-1/{group}/round-1/trace.jsonl'
            context = next((e['data'] for line in trace.read_text().splitlines()
                            if (e := json.loads(line))['event'] == 'context'), None)
            if context is None:
                raise ValueError(f'缺少首轮上下文，不能做不同样本的平均：{task}/{group}')
            first_contexts.append(context['estimated_tokens'])
        base = [e for e in events if e['group'] == group]
        groups[group] = dict(
            completed=len(valid), passed=passed, success_rate=passed / len(TASKS),
            valid_final_mean=statistics.mean(valid) if valid else None,
            delivery_score=sum(valid) / len(TASKS),
            common_mean=statistics.mean(runs[t, group]['rounds'][-1]['grade']['score'] for t in common),
            online_six=usage([e for e in base if e['stage'] == 'online' and e.get('task_id') in TASKS]),
            online_common=usage([e for e in base if e['stage'] == 'online' and e.get('task_id') in common]),
            build=usage([e for e in base if e['stage'] == 'build']),
            judge_six=usage([e for e in base if e['stage'] == 'judge' and e.get('task_id') in TASKS]),
            first_context_mean=statistics.mean(first_contexts), first_context_tokens=first_contexts)
    return dict(run_id=run.name, currency=report['usage']['currency'],
                protocol=report['protocol'], settings=report['settings'],
                source_hashes={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (report_path, usage_path)},
                tasks=TASKS, common_tasks=common, groups=groups, scores=scores,
                definitions=dict(delivery_score='六题最终有效分之和/6；未完成按0计，仅表示端到端交付，不是记忆正确率',
                                 success='三轮执行和评分全部成功且最终达到原 rubric 阈值',
                                 tokens='输入含缓存读/写，仅计 online 的 Agent+记忆维护；输出按供应商 usage',
                                 common='所有四组完成的同一组题，不要求每组均达标',
                                 audit='理疗通勤评分争议保留原分，文档单列敏感性分析'))


def configure_plots():
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import font_manager, pyplot as plt
    font_path = Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
    if font_path.exists():
        font_manager.fontManager.addfont(str(font_path))
        font = font_manager.FontProperties(fname=font_path).get_name()
    else:
        font = 'Noto Sans CJK SC'
    plt.rcParams.update({'font.family': [font, 'DejaVu Sans'], 'font.size': 11,
                         'axes.unicode_minus': False, 'axes.spines.top': False,
                         'axes.spines.right': False, 'axes.labelcolor': INK,
                         'text.color': INK, 'xtick.color': INK, 'ytick.color': INK,
                         'axes.edgecolor': '#d1d9e2', 'figure.facecolor': '#ffffff',
                         'axes.facecolor': '#ffffff', 'svg.fonttype': 'path'})
    return plt


def style_axis(ax):
    ax.set_axisbelow(True)
    ax.grid(axis='y', color='#e8edf2', linewidth=0.8)
    ax.tick_params(axis='both', length=0)
    for label in ax.get_xticklabels():
        if label.get_text() == 'LaMemory':
            label.set_color(COLORS[-1])
            label.set_fontweight('bold')


def save_plot(plt, fig, output, name, title, subtitle, footnote):
    fig.suptitle(title, x=0.06, y=0.98, ha='left', fontsize=19, fontweight='bold')
    fig.text(0.06, 0.89, subtitle, fontsize=11, color='#587086')
    fig.text(0.06, 0.025, footnote, fontsize=9, color='#587086', va='bottom')
    fig.subplots_adjust(left=0.07, right=0.97, bottom=0.16, top=0.79, wspace=0.28)
    for ext in ('png', 'svg'):
        fig.savefig(output / f'{name}.{ext}', dpi=190, facecolor='white')
    plt.close(fig)


def plot(data, output):
    plt = configure_plots()
    import numpy as np
    g = data['groups']
    records = {(r['task_id'], r['group']): r for r in data['scores']}
    fig, ax = plt.subplots(figsize=(15.2, 6.4))
    x, width = np.arange(len(TASKS)), 0.19
    for i, group in enumerate(GROUPS):
        positions = x + (i - 1.5) * width
        vals = [records[t, group]['score'] for t in TASKS]
        bars = ax.bar(positions, [v if v is not None else 0 for v in vals], width,
                      color=COLORS[i], label=LABELS[i], zorder=3)
        for bar, value, pos in zip(bars, vals, positions):
            if value is None:
                ax.scatter([pos], [5], marker='x', color=COLORS[i], s=55, linewidth=2)
                ax.text(pos, 11, '未完成', ha='center', fontsize=8, rotation=90)
            else:
                ax.text(pos, value + 1.5, f'{value:g}', ha='center', fontsize=9,
                        fontweight='bold' if group == 'lamemory' else 'normal')
    ax.set_xticks(x, TASKS.values()); ax.set_ylim(0, 115); ax.set_ylabel('最终有效得分 / 100')
    ax.legend(ncol=4, loc='upper center', bbox_to_anchor=(0.5, 1.18), frameon=False)
    style_axis(ax)
    save_plot(plt, fig, output, '01_task_scores', 'LaMemory 在六个记忆任务上全部完成交付',
              '逐题保留原始有效评分；LaMemory 在 SQL、理疗和项目风险三题达到 100 分',
              '每题仅一次独立重复。× 表示输出截断导致未完成，不是任务得分为零。理疗评分有争议，未改写原分。')

    fig, axes = plt.subplots(1, 2, figsize=(13.6, 6.0))
    for ax, values, ylabel, suffix in [
        (axes[0], [g[k]['success_rate'] * 100 for k in GROUPS], '完整通过率 / %', '%'),
        (axes[1], [g[k]['delivery_score'] for k in GROUPS], '端到端有效得分 / 100', '')]:
        ax.bar(LABELS, values, color=COLORS, width=0.62, zorder=3)
        ax.set_ylim(0, 120); ax.set_ylabel(ylabel); style_axis(ax)
        for i, val in enumerate(values):
            ax.text(i, val + 2.5, f'{val:.1f}{suffix}', ha='center', fontsize=13, fontweight='bold')
            if suffix:
                ax.text(i, val / 2, f"{g[GROUPS[i]]['passed']}/6", ha='center', color='white', fontsize=12)
    save_plot(plt, fig, output, '02_delivery', 'LaMemory：100% 通过率，端到端有效得分 96.0',
              '成功率统一以六题为分母；端到端有效得分将未完成任务按 0 计入，强调实际交付能力',
              '端到端有效得分是派生交付指标，不是原始评分或记忆正确率；单次结果不代表统计显著优势。')

    fig, axes = plt.subplots(1, 2, figsize=(14.4, 6.3))
    parts = [('agent_input', 'Agent 输入', 1.0), ('agent_output', 'Agent 输出', 0.65),
             ('maintenance_tokens', '记忆维护输入+输出', 0.32)]
    for ax, scope, label in [(axes[0], 'online_common', '相同五题在线 Token / 千'),
                             (axes[1], 'online_six', '六题全部尝试在线 Token / 千')]:
        bottom = np.zeros(4)
        for key, part, alpha in parts:
            values = np.array([g[k][scope][key] / 1000 for k in GROUPS])
            ax.bar(LABELS, values, bottom=bottom, color=COLORS, alpha=alpha,
                   edgecolor='white', linewidth=1, label=part, width=0.6, zorder=3)
            bottom += values
        for i, val in enumerate(bottom):
            ax.text(i, val + 15, f'{val:,.1f}k', ha='center', fontsize=12, fontweight='bold')
        ax.set_ylim(0, max(bottom) * 1.33); ax.set_ylabel(label); style_axis(ax)
    axes[1].legend(loc='upper right', frameon=False, fontsize=9)
    saving = 1 - g['lamemory']['online_common']['total_tokens'] / g['legacy']['online_common']['total_tokens']
    save_plot(plt, fig, output, '03_tokens', f'相同五题，LaMemory 比 legacy 少用 {saving:.1%} Token',
              '左：共同完成五题的可比资源；右：六题全部尝试，含失败消耗（各组完成量不同）',
              '统计 Agent 与在线记忆维护，排除建库和评分。输入含缓存读写。配对五题 LaMemory 比完整历史少约 1.0%。')

    fig, axes = plt.subplots(1, 2, figsize=(13.6, 6.0))
    online = [g[k]['online_common']['cost'] for k in GROUPS]
    axes[0].bar(LABELS, online, color=COLORS, width=0.6, zorder=3)
    axes[0].set_ylabel(f"相同五题在线费用 / {data['currency']}")
    for i, val in enumerate(online):axes[0].text(i, val + .05, f'¥{val:.2f}', ha='center', fontsize=13, fontweight='bold')
    axes[0].set_ylim(0, max(online) * 1.22); style_axis(axes[0])
    all_six = [g[k]['online_six']['cost'] for k in GROUPS]
    build = [g[k]['build']['cost'] for k in GROUPS]
    axes[1].bar(LABELS, all_six, color=COLORS, width=0.6, label='六题在线（含失败尝试）', zorder=3)
    axes[1].bar(LABELS, build, bottom=all_six, color=COLORS, alpha=.32, width=.6,
                edgecolor='white', label='一次共同历史建库', zorder=3)
    for i, val in enumerate(np.array(all_six) + build):
        axes[1].text(i, val + .05, f'¥{val:.2f}', ha='center', fontsize=13, fontweight='bold')
    axes[1].set_ylabel(f"六题建库+在线费用 / {data['currency']}")
    axes[1].set_ylim(0, max(np.array(all_six) + build) * 1.28)
    axes[1].legend(loc='upper right', frameon=False, fontsize=9); style_axis(axes[1])
    cost_saving = 1 - online[-1] / online[1]
    save_plot(plt, fig, output, '04_cost', f'相同五题，LaMemory 在线费用比 legacy 低 {cost_saving:.1%}',
              '左：控制题目与完成情况的费用比较；右：六题全部尝试的实际资源支出，建库单列',
              '未计评分费用；按运行价格快照核算，非供应商账单。完整历史费用更低，但仅完成六题中的五题。')

    fig, ax = plt.subplots(figsize=(12.6, 5.8))
    values = [g[k]['first_context_mean'] for k in GROUPS]
    ax.bar(LABELS, values, color=COLORS, width=.6, zorder=3)
    for i, val in enumerate(values):ax.text(i, val + 90, f'{val:,.0f}', ha='center', fontsize=13, fontweight='bold')
    ax.set_ylim(0, max(values) * 1.22); ax.set_ylabel('首轮第一次请求的估算输入 Token（六题平均）'); style_axis(ax)
    saving = 1 - values[-1] / values[2]
    save_plot(plt, fig, output, '05_initial_context', f'LaMemory 初始上下文比完整历史小 {saving:.1%}',
              '同一六题首轮第一次请求，含系统提示、工具定义、当前请求及注入记忆',
              '此项为运行器 UTF-8/3 估算，不是供应商 usage；不能直接外推为整项任务或账单同比节省。')

    fig, ax = plt.subplots(figsize=(15.2, 6.4))
    x, width = np.arange(len(TASKS)), .19
    maximum = 0
    for i, group in enumerate(GROUPS):
        positions = x + (i - 1.5) * width
        values = [records[t, group]['online']['total_tokens'] / 1000 for t in TASKS]
        maximum = max(maximum, max(values))
        bars = ax.bar(positions, values, width, color=COLORS[i], label=LABELS[i], zorder=3)
        for task, bar, value, pos in zip(TASKS, bars, values, positions):
            if not records[task, group]['completed']:
                bar.set_hatch('///'); bar.set_edgecolor('#576578'); bar.set_alpha(.55)
            ax.text(pos, value + 4, f'{value:.0f}k', ha='center', fontsize=8)
    ax.set_xticks(x, TASKS.values()); ax.set_ylim(0, maximum * 1.2)
    ax.set_ylabel('逐题在线总 Token / 千'); style_axis(ax)
    ax.legend(ncol=4, loc='upper center', bbox_to_anchor=(.5, 1.18), frameon=False)
    save_plot(plt, fig, output, '06_task_tokens', '六个记忆任务的逐题 Token 消耗',
              'Agent 与在线记忆维护的实际输入+输出；斜线柱表示未完成任务的已消耗 Token',
              '季度题只有 LaMemory 完成三轮，其余三组输出截断；不能把失败早停的低 Token 视为效率优势。排除建库和评分。')


def markdown(data, output):
    g, scores = data['groups'], {(x['task_id'], x['group']): x for x in data['scores']}
    relative_run = Path('../../.myclaude/benchmarks') / data['run_id']
    la, legacy, full = (g[k] for k in ('lamemory', 'legacy', 'full_history'))
    token_saving = 1 - la['online_common']['total_tokens'] / legacy['online_common']['total_tokens']
    cost_saving = 1 - la['online_common']['cost'] / legacy['online_common']['cost']
    full_cost_delta = la['online_common']['cost'] / full['online_common']['cost'] - 1
    lines = [
        '# LaMemory 六个记忆任务评测汇总', '',
        f"来源运行：`{data['run_id']}`。仅纳入六个记忆任务，每题一次独立重复、三轮固定任务对话。", '',
        '> LaMemory 是本轮唯一完成并通过全部六题的方法，最终有效均分为 96.0；'
        f'在四组共同完成的五题上，在线 Token 比 legacy 少 {token_saving:.1%}，在线费用低 {cost_saving:.1%}。'
        '这体现了当前样本下的交付完整性与相对旧系统的资源优势，不代表所有记忆质量或成本指标均最优。', '',
        '## 1. 范围与共同条件', '',
        '- 范围：父母生日、理疗安排、SQL 学习时间块、项目风险看板、季度学习计划、财务复盘。'
        '不纳入非记忆任务的得分、调用、Token、耗时或费用。',
        '- 四组：none（无长期记忆）、legacy（完整注入提取后的 Markdown）、full_history（完整历史用户原文）、LaMemory（检索事实与分层摘要）。',
        '- Agent/评分窗口 65536、输出上限 8192；每轮最多 8 次模型和 24 次工具调用；shared_strict 保留当前任务完整会话，超限明确失败。',
        '- LaMemory 使用原请求 hybrid 检索，top-k=10、记忆预算 2048；主动记忆工具、静态 Skill、重排及 M3 自动学习关闭。',
        '- 44 条共同历史记录一次建库，legacy 和 LaMemory 各成功提取 44 次；本轮没有再次出现来源校验建库失败。', '',
        '## 2. 交付结果：LaMemory 六题全部通过', '',
        '![六个任务的最终有效评分](figures/01_task_scores.png)', '',
        '| 任务 | none | legacy | full_history | LaMemory |', '|---|---:|---:|---:|---:|']
    for task, name in TASKS.items():
        values = ['未完成（输出截断）' if scores[task, k]['score'] is None else f"{scores[task,k]['score']:g}" for k in GROUPS]
        lines.append('| ' + name + ' | ' + ' | '.join(values) + ' |')
    lines.extend(['', '![通过率与端到端有效得分](figures/02_delivery.png)', '',
                  '| 方法 | 完整完成 | 达标通过 | 有效样本最终均分 | 端到端有效得分 |', '|---|---:|---:|---:|---:|'])
    for k in GROUPS:
        row = g[k]
        lines.append(f"| {k} | {row['completed']}/6 | {row['passed']}/6（{row['success_rate']:.1%}） | "
                     f"{row['valid_final_mean']:.2f}（n={row['completed']}） | {row['delivery_score']:.2f} |")
    lines.extend(['', '**口径：**完整完成要求三轮执行与评分均成功；通过还要求最终达到原 rubric 阈值。'
                  '端到端有效得分＝六题最终有效分之和÷6，未完成按 0 计，是本汇总明确定义的交付指标；'
                  '不改写原报告，不表示失败任务的记忆质量或已有产物得分为零。各组条件均分分母不同，不可直接据此排名。', '',
                  'LaMemory 在理疗、SQL、项目风险三题取得 100 分；季度计划 94 分，其他三组在此题因输出截断未完成。'
                  '全六题相对 legacy 的通过率提升为 16.7 个百分点，端到端有效得分提升 15.0 分。'
                  '无长期记忆组仅财务复盘达到总分阈值，不能将其 79 分解释为召回了历史事实。', '',
                  '## 3. 资源比较：相同题目与全部尝试分开', '',
                  '### 3.1 四组共同完成的五题', '',
                  '配对范围为生日、理疗、SQL、项目风险、财务五题。共同完成不等于共同达标；none 的多数题仍缺少历史事实。'
                  '五题三轮全部执行完毕，用于避免季度题失败早停造成的资源分母差异。', '',
                  '![配对五题及全部六题的在线 Token](figures/03_tokens.png)', '',
                  '| 方法 | 五题有效均分 | 在线输入 Token | 在线输出 Token | 在线总 Token | 在线费用/CNY |', '|---|---:|---:|---:|---:|---:|'])
    for k in GROUPS:
        row, u = g[k], g[k]['online_common']
        lines.append(f"| {k} | {row['common_mean']:.2f} | {u['input_tokens']:,} | {u['output_tokens']:,} | "
                     f"{u['total_tokens']:,} | {u['cost']:.4f} |")
    lines.extend(['', f'LaMemory 相对 legacy：在线 Token 减少 **{token_saving:.1%}**，费用减少 **{cost_saving:.1%}**；'
                  f"原评分均分 {la['common_mean']:.1f} 对 {legacy['common_mean']:.1f}，相差 -{legacy['common_mean']-la['common_mean']:.1f} 分。"
                  f'相对 full_history：Token 低约 1.0%，但费用高 **{full_cost_delta:.1%}**。'
                  '完整历史组本轮缓存利用、调用次数和零在线记忆维护费用共同影响账单，Token 与金额不能等同。', '',
                  '### 3.2 六题全部尝试的实际资源', '',
                  '![在线与建库费用](figures/04_cost.png)', '',
                  '| 方法 | 六题在线 Token（含失败） | 六题在线费用 | 一次建库费用 | 建库+六题在线 | 六题评分费用（另列） |',
                  '|---|---:|---:|---:|---:|---:|'])
    for k in GROUPS:
        u, b, j = g[k]['online_six'], g[k]['build'], g[k]['judge_six']
        lines.append(f"| {k} | {u['total_tokens']:,} | ¥{u['cost']:.4f} | ¥{b['cost']:.4f} | "
                     f"¥{u['cost']+b['cost']:.4f} | ¥{j['cost']:.4f} |")
    total = sum(row['online_six']['cost'] + row['build']['cost'] + row['judge_six']['cost'] for row in g.values())
    lines.extend(['', f'六题在线、一次共同建库与六题评分核算合计 **¥{total:.4f}**。评分是评测开销，不纳入产品运行成本。'
                  '建库是共同初始历史的独立支出，在此完整列出一次，没有分摊或重复累计。'
                  '输入 Token 包含缓存读写；在线统计含 Agent、提取、摘要、查询及维护向量。费用按原运行价格快照与 provider usage 核算，不是供应商账单。', '',
                  '六题全部尝试的原始资源总量不可当作相同工作量的效率排名：LaMemory 实际完成了六题三轮，其他组只完成五题三轮。'
                  '本报告同时展示配对五题与全部六题，避免借失败早停制造低成本优势。', '',
                  '![六个任务逐题在线 Token](figures/06_task_tokens.png)', '',
                  '逐题图使用相同的六题 online 统计；斜线表示未完成的尝试。季度题中只有 LaMemory 完成三轮，'
                  '因此该题各柱代表的有效交付量不同，不适合直接比较效率。', '',
                  '### 3.3 初始上下文开销', '',
                  '![首轮第一次请求的上下文](figures/05_initial_context.png)', '',
                  '该图对六题首轮第一次请求做平均，说明检索式注入能减少初始输入；属于 UTF-8/3 估算，含系统提示、工具和当前请求。'
                  '它不等于整项任务累计 Token 节省：后续多次写文件与复查会重发大量会话和工具内容。', '',
                  '## 4. LaMemory 的优势与剩余缺口', '',
                  '### 已观察到的优势', '',
                  '1. **交付覆盖最完整：6/6 通过。** 本轮所有六题完成三轮，季度计划也完整交付。',
                  '2. **相对旧系统的资源效率。** 相同五题在线 Token 少约 23.2%、费用少约 35.3%，原评分接近。',
                  '3. **精确日程与任务约束可用于实际交付。** 理疗、SQL、项目风险最终均为 100 分；无长期记忆组在这些任务上明显落后。',
                  '4. **注入上下文较小。** 相比完整历史，首轮输入显著减少，为更长历史下的规模测试提供了值得继续验证的方向。', '',
                  '### 不能忽略的缺口', '',
                  '- 生日：明确的“父亲按摩仪尚未购买”已在记忆库中，却未进入该任务注入；召回了较早泛化购买计划，最终礼物仍标待确认，扣 10 分。',
                  '- 财务：30000 元明确基准已保存但未注入，概览仅有“3万出头”；未明确交付 37.5% 和 50000 元，扣 8 分。',
                  '- 季度：设计心理学、UI 沟通和研发协调的历史事实覆盖不足，另有计划算术与行动项的小问题；最终 94 分不能解释为全部事实均被覆盖。',
                  '- 完整历史组在本轮配对五题上的费用更低，条件评分略高。当前不能宣称 LaMemory 在所有质量或费用指标上全面胜出。', '',
                  '## 5. 评分复核与解释边界', '',
                  '**理疗题存在明显评分争议。** full_history 产物写“18:00 出发→40分钟路程→18:40到达→19:00开始”；'
                  '评分器却因到达后只有20分钟额外缓冲而扣了15分，与 rubric 给出的18:00至18:40通勤预留示例不一致。'
                  '主表与所有图均保留原评分，未利用该争议更改原始证据。', '',
                  '仅作敏感性分析：若只将该项恢复15分，full_history 配对五题均分由97.0变为100.0，'
                  '六题端到端有效得分由80.83变为83.33，仍为5/6通过；LaMemory的6/6覆盖与资源统计不受此调整影响。'
                  '这说明 LaMemory 的全题覆盖优势比小幅平均分差更值得强调。', '',
                  '- none、legacy、full_history 的季度失败均为输出达到8192的截断，不能据此断言其记忆召回一定错误。',
                  '- 每题仅一次独立重复，三轮复查不是三个独立样本；未建立统计显著性，也不能推导更大历史规模的收益。',
                  '- 六题仍是开发任务；manual facts 改写没有在本次启用，本次使用原请求查询。评分变化、任务生成与记忆检索的作用应分开。', '',
                  '## 6. 证据与复现', '',
                  f'- [原始运行报告]({relative_run}/report.json)、[逐请求用量]({relative_run}/usage.jsonl)。原始文件未修改。',
                  '- [筛选数据与来源 SHA-256](comparison-data.json)、[方法级 CSV](metrics.csv)、[逐题 CSV](scores.csv)。',
                  '- `figures/` 同时提供 PNG 与 SVG，可直接用于汇报；绿色突出 LaMemory，所有坐标从0开始，并公开失败及分母。',
                  '- 绘图依赖 matplotlib；不调用模型或网络，不写记忆库。', '',
                  '```bash',
                  'MPLCONFIGDIR=/tmp/lamemory-mpl-cache .venv/bin/python -m benchmarks.memory_benchmark.plot_memory_results \\',
                  f'  --run .myclaude/benchmarks/{data["run_id"]} \\',
                  f'  --output docs/{output.name}',
                  '```', ''])
    (output / 'README.md').write_text('\n'.join(lines), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run, output = args.run.resolve(), args.output.resolve()
    if output == run or output.is_relative_to(run):
        raise ValueError('不能把衍生产物写入原始运行证据目录')
    data = aggregate(run)
    output.mkdir(parents=True, exist_ok=True)
    figures = output / 'figures'; figures.mkdir(exist_ok=True)
    (output / 'comparison-data.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    with (output / 'metrics.csv').open('w', encoding='utf-8-sig', newline='') as f:
        fields = ['group', 'completed', 'passed', 'success_rate', 'valid_final_mean', 'delivery_score',
                  'common_mean', 'all6_online_tokens', 'common_online_tokens', 'all6_online_cost', 'common_online_cost', 'build_cost']
        writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader()
        for group, row in data['groups'].items():
            writer.writerow({**{k: row[k] for k in fields if k in row}, 'group': group,
                             'all6_online_tokens': row['online_six']['total_tokens'],
                             'common_online_tokens': row['online_common']['total_tokens'],
                             'all6_online_cost': row['online_six']['cost'],
                             'common_online_cost': row['online_common']['cost'], 'build_cost': row['build']['cost']})
    with (output / 'scores.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['task_id', 'task', 'group', 'completed', 'passed', 'score'])
        writer.writeheader()
        writer.writerows({k: v for k, v in r.items() if k not in {'rounds', 'online'}} for r in data['scores'])
    plot(data, figures)
    markdown(data, output)
    print(json.dumps(dict(report=str(output / 'README.md'), charts=6, tasks=len(TASKS),
                          matched_tasks=len(data['common_tasks'])), ensure_ascii=False))


if __name__ == '__main__':
    main()
