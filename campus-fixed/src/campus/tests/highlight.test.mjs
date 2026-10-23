import test from 'node:test';
import assert from 'node:assert/strict';
import { highlightRuns } from '../highlight.js';

const joined = (runs) => runs.map((run) => run.text).join('');
const marked = (runs) => runs.filter((run) => run.match).map((run) => run.text);

test('marked runs always reassemble into the original excerpt', () => {
  // The excerpt is registered as evidence and verified by substring, so a
  // highlight that changed a character would make the quote unverifiable.
  const cases = [
    ['符合条件的学生可申请奖学金，材料于九月前提交。', '奖学金申请材料'],
    ['GPA 3.5 以上可申请，详见通知。', 'gpa'],
    ['本段与查询词无关。', '奖学金'],
    ['短', '奖学金'],
    ['', '奖学金'],
    ['有内容', ''],
  ];
  for (const [text, query] of cases) {
    assert.equal(joined(highlightRuns(text, query)), text, `${text} / ${query}`);
  }
});

test('a whole-word query marks exactly that word', () => {
  assert.deepEqual(marked(highlightRuns('关于奖学金评审的通知', '奖学金')), ['奖学金']);
});

test('a long Chinese query still marks the parts that appear', () => {
  // Chinese has no word boundaries and the server matches substrings, so a
  // reader who typed a whole phrase must still see the fragments that matched.
  // Touching fragments merge, so "申请" and "奖学金" come back as one mark.
  assert.deepEqual(
    marked(highlightRuns('符合条件的学生可申请奖学金，材料于九月前提交。', '奖学金申请材料')),
    ['申请奖学金', '材料'],
  );
});

test('ASCII matching ignores case', () => {
  assert.deepEqual(marked(highlightRuns('GPA 3.5 以上', 'gpa')), ['GPA']);
});

test('a single character is not worth marking', () => {
  assert.deepEqual(marked(highlightRuns('学籍管理办法', '学生')), []);
});

test('adjacent matches merge into one run so no seam can show', () => {
  const runs = highlightRuns('奖学金材料', '奖学金材料');
  assert.deepEqual(runs, [{ text: '奖学金材料', match: true }]);
});

test('an unmatched excerpt stays one plain run', () => {
  assert.deepEqual(highlightRuns('本段与查询词无关。', '奖学金'), [{ text: '本段与查询词无关。', match: false }]);
});

test('a query with no match leaves the text untouched', () => {
  assert.deepEqual(marked(highlightRuns('本段与查询词无关。', '竞赛')), []);
});
