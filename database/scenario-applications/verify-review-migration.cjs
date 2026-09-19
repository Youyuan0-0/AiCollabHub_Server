// 在独立内存 PostgreSQL 中验证迁移、历史保留及七项评分，不读取 .env。
const { PGlite } = require('@electric-sql/pglite');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

async function main() {
  const db = new PGlite();
  try {
    await db.exec(`CREATE TABLE scene_review_note (
      id text PRIMARY KEY, recommendation text, reason text DEFAULT '',
      completeness_score smallint, usability_score smallint, security_score smallint,
      minimum_score smallint NOT NULL);
      INSERT INTO scene_review_note VALUES ('old-approved','approved','历史通过',3,4,5,3),
        ('old-rejected','rejected','历史驳回',NULL,NULL,NULL,3);`);
    const before = (await db.query('SELECT * FROM scene_review_note ORDER BY id')).rows;
    const sql = fs.readFileSync(path.join(__dirname, 'review-seven-scores-20260918.sql'), 'utf8');
    await db.exec(sql);
    await db.exec(sql);
    const after = (await db.query('SELECT * FROM scene_review_note ORDER BY id')).rows;
    assert.deepEqual(after.map(({ score_schema, quality_scores, ...row }) => row), before);
    assert.ok(after.every(row => row.score_schema === 1 && row.quality_scores === null));
    const scores = Object.fromEntries(['formatCompliance', 'safetyCompliance', 'annotationCompliance',
      'structuralCompleteness', 'authenticity', 'consistency', 'cleanliness'].map(key => [key, 1]));
    await db.query(`INSERT INTO scene_review_note(id,recommendation,score_schema,quality_scores)
      VALUES ('new-low','approved',2,$1)`, [JSON.stringify(scores)]);
    assert.equal((await db.query("SELECT score_schema FROM scene_review_note WHERE id='new-low'")).rows[0].score_schema, 2);
    for (const invalid of [null, {}, { ...scores, cleanliness: 0 }, { ...scores, cleanliness: 6 },
      { ...scores, cleanliness: true }, { ...scores, cleanliness: 1.5 },
      { ...scores, cleanliness: '1' }, { ...scores, extra: 1 }]) {
      await assert.rejects(db.query(`INSERT INTO scene_review_note(id,recommendation,score_schema,quality_scores)
        VALUES ('invalid','approved',2,$1)`, [JSON.stringify(invalid)]), error => error.code === '23514');
    }
    console.log('PASS migration repeated, legacy history preserved, seven low scores accepted, invalid scores rejected');
  } finally { await db.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
