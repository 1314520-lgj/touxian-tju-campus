/** Rules run only in the browser. The raw text is never sent to the server. */
export function extractLocal(text) {
  const clean = text.trim();
  const lines = clean.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  const datePattern = /(?:20\d{2})\s*[-年/.]\s*(?:0?[1-9]|1[0-2])\s*[-月/.]\s*(?:0?[1-9]|[12]\d|3[01])\s*日?(?!\d)/;
  const deadlineLine = lines.find((line) => /截止|之前|前\s*提交|最晚|截至|deadline/i.test(line) && datePattern.test(line));
  const dateMatch = (deadlineLine || clean).match(datePattern);
  let deadline_date = null;
  if (dateMatch) {
    const parts = dateMatch[0].match(/\d+/g);
    const date = parts[0] + '-' + parts[1].padStart(2, '0') + '-' + parts[2].padStart(2, '0');
    const parsed = new Date(date + 'T00:00:00+08:00');
    if (!Number.isNaN(parsed.getTime()) && new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(parsed) === date) deadline_date = date;
  }
  const deadlineContext = Boolean(deadlineLine);
  const urls = [...new Set((clean.match(/https?:\/\/[^\s<>"'，。；）\]]+/g) || []))].filter((url) => {
    try { new URL(url); return true; } catch { return false; }
  });
  const actions = lines.filter((line) => /提交|报名|填写|上传|领取|参加|缴费|确认|完成/.test(line)).slice(0, 8);
  const materials = lines.filter((line) => /材料|附件|成绩单|证明|身份证|申请表/.test(line)).slice(0, 8);
  const warnings = [];
  if ((clean.match(/20\d{2}[-年/.]/g) || []).length > 1) warnings.push('原文含多个日期，请核对提取的日期是否为截止时间。');
  if (/明天|后天|本周|下周|周[一二三四五六日天]|\d{1,2}月\d{1,2}日/.test(clean) && !dateMatch) warnings.push('发现相对日期或缺少年份的日期，请人工填写完整截止日期。');
  if (dateMatch && !deadlineContext) warnings.push('原文中的日期未明确是截止时间，请核对后再保存。');
  if (dateMatch && !deadline_date) warnings.push('发现无法识别的日期，请人工填写。');
  if (!actions.length) warnings.push('没有识别到明确行动，请补充需要完成的步骤。');
  return {
    draft: { title: (lines[0] || '').slice(0, 300), action_steps: actions, materials, entry_urls: urls, deadline_at: null, deadline_date: deadlineContext ? deadline_date : null, timezone: 'Asia/Shanghai', evidence_excerpt: null, excerpt_sync_consent: false },
    warnings,
  };
}
export function draftFromFields(fields) {
  const split = (value) => value.split('\n').map((s) => s.trim()).filter(Boolean);
  return {
    title: fields.title.trim(), action_steps: split(fields.actions), materials: split(fields.materials), entry_urls: split(fields.urls),
    deadline_at: fields.date && fields.time ? new Date(fields.date + 'T' + fields.time + ':00+08:00').toISOString() : null,
    deadline_date: fields.date && !fields.time ? fields.date : null, timezone: 'Asia/Shanghai',
    evidence_excerpt: fields.consent ? fields.excerpt.slice(0, 2000) || null : null, excerpt_sync_consent: Boolean(fields.consent),
  };
}
export function fieldsFromDraft(draft) {
  let date = draft.deadline_date || '', time = '';
  if (draft.deadline_at) {
    const parts = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(draft.deadline_at)).split(' ');
    [date, time] = parts;
  }
  return { title: draft.title, actions: draft.action_steps.join('\n'), materials: draft.materials.join('\n'), urls: draft.entry_urls.join('\n'), date, time, excerpt: draft.evidence_excerpt || '', consent: Boolean(draft.excerpt_sync_consent) };
}

