def clean(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("@", "＠").replace("<", "&lt;").replace(">", "&gt;")

def render(report, rubric):
    meta = report["metadata"]
    lines = ["# Судья — отчёт", "", f"Статус: {report['status']}",
             f"Версия: `{meta['target_sha']}`; запрос: `{meta['request_id']}`", "",
             clean(report.get("summary", "")), ""]
    if "totals" in report:
        t = report["totals"]
        lines += [f"Баллы: {t['earned']} из {t['assessed_max']} рассмотренных. Не оценено: {t['unassessed_max']} баллов.",
                  "Это предварительная оценка, не прогноз решения жюри.", "",
                  "| Критерий | Балл | Доказательства | Обоснование / улучшение |", "|---|---:|---|---|"]
        maxima = {c['id']:c['max'] for c in rubric['criteria']}
        for c in report['criteria']:
            score = 'нет данных' if c['score'] is None else str(c['score'])+'/'+str(maxima[c['id']])
            lines.append(f"| {c['id']} | {score} | {clean(', '.join(c['evidence_ids']))} | {clean(c['reason'])}; {clean(c['improvement'])} |")
    for key,title in [('blockers','Препятствия'),('improvements','Улучшения'),('questions','Вопросы защиты'),('limitations','Ограничения')]:
        lines += ['', '## '+title, '']
        for item in report.get(key, []):
            lines.append('- '+clean(item if isinstance(item,str) else '; '.join(f'{k}: {v}' for k,v in item.items())))
    return '\n'.join(lines)+'\n'
