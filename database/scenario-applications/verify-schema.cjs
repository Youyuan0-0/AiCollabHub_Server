// 设计验证：独立 PGlite 内存实例。通过 NODE_PATH 提供工具依赖，不读取 .env。
const { PGlite } = require('@electric-sql/pglite');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');

async function main() {
  const db = new PGlite();
  const sql = fs.readFileSync(process.argv[2] || path.join(__dirname, 'schema.sql'), 'utf8');
  const checks = [];
  const query = async (sql) => (await db.query(sql)).rows;
  const check = async (name, fn) => { await fn(); checks.push(name); console.log(`PASS ${name}`); };
  const reject = async (sql, code) => {
    let error;
    await db.exec('BEGIN');
    try { await db.exec(sql); await db.exec('SET CONSTRAINTS ALL IMMEDIATE'); }
    catch (e) { error = e; }
    finally { await db.exec('ROLLBACK'); }
    assert.ok(error, '预期数据库拒绝写入');
    assert.equal(error.code, code, error.message);
  };
  const activity = (id, op, mode, status, request = `request-${id}`, app = 'app-a') => `
    INSERT INTO scene_activity(id,application_id,actor_subject,operation,request_id,request_fingerprint,
      mode,status,completed_at,duration_ms)
    VALUES ('${id}','${app}','user-a','${op}','${request}',repeat('a',64),'${mode}','${status}',
      ${['succeeded','failed','timed_out','unchanged'].includes(status) ? 'now()' : 'NULL'},100)`;
  try {
    await check('10 张业务表和 2 个视图可完整创建', async () => {
      await db.exec(sql);
      assert.equal((await query("SELECT * FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")).length, 10);
      assert.equal((await query("SELECT * FROM information_schema.views WHERE table_schema='public'")).length, 2);
      assert.equal((await query('SELECT * FROM scene_configuration')).length, 2);
    });
    await check('未取得外部编号时允许登记本地应用', async () => {
      await db.exec("INSERT INTO scene_application(id,created_by_subject) VALUES ('app-a','user-a'),('app-b','user-b')");
    });
    await check('Agent Dev 绑定字段必须完整', () => reject(
      "UPDATE scene_application SET agent_code='A001' WHERE id='app-a'", '23514'));
    await check('同编号跨环境允许区分', async () => {
      await db.exec(`UPDATE scene_application SET agent_profile='agent-test',workspace_code='ws',agent_code='A001',
        market_profile='market-test',market_resource_id='R001',market_listing_id='L001' WHERE id='app-a';
        UPDATE scene_application SET agent_profile='agent-prod',workspace_code='ws',agent_code='A001' WHERE id='app-b'`);
    });
    await check('同环境和工作空间不可重复绑定', () => reject(
      "UPDATE scene_application SET agent_profile='agent-test' WHERE id='app-b'", '23505'));
    await check('HiMarket Listing 不可登记到两个应用', () => reject(`
      UPDATE scene_application SET market_profile='market-test',market_resource_id='R001',market_listing_id='L001'
      WHERE id='app-b'`, '23505'));
    await check('外部目录、版本、正文、协议和授权不能写进本地扩展', async () => {
      for (const field of ['name','summary','descriptionMarkdown','category','categoryId','tags',
        'version','inputSchema','outputSchema','communicationProtocol','capabilities','status','activeGrant']) {
        await reject(`UPDATE scene_application SET local_metadata='{"${field}":"外部数据"}' WHERE id='app-a'`, '23514');
      }
    });
    await check('本地联系信息和结构化教程链接可以保存', async () => {
      await db.exec(`UPDATE scene_application SET local_metadata='{"contact":"平台联系人",
        "tutorials":[{"title":"内部使用说明","kind":"document","url":"/files/guide"}]}' WHERE id='app-a'`);
    });
    await check('指定组织开放必须有目标', () => reject(
      "UPDATE scene_application SET visibility_scope='organizations' WHERE id='app-a'", '23514'));
    await check('范围和目标可在同一事务切换', async () => {
      await db.exec(`BEGIN; UPDATE scene_application SET visibility_scope='organizations' WHERE id='app-a';
        INSERT INTO scene_visibility_target(application_id,scope,target_id) VALUES ('app-a','organizations','org-a'); COMMIT`);
    });
    await check('不能删除最后一个指定范围目标', () => reject(
      "DELETE FROM scene_visibility_target WHERE application_id='app-a'", '23514'));
    await check('范围目标类别必须一致', () => reject(
      "INSERT INTO scene_visibility_target(application_id,scope,target_id) VALUES ('app-a','members','user-a')", '23503'));
    await check('允许事务内恢复全部开放', async () => {
      await db.exec(`BEGIN; DELETE FROM scene_visibility_target WHERE application_id='app-a';
        UPDATE scene_application SET visibility_scope='all' WHERE id='app-a'; COMMIT`);
    });
    await check('可以保存空白补充草稿', async () => {
      await db.exec(`INSERT INTO scene_submission_extension(id,application_id,submission_key,created_by_subject)
        VALUES ('extra-a','app-a','publication-a','user-a'),('extra-b','app-b','publication-b','user-b')`);
    });
    await check('补充草稿不能重新保存外部草稿字段', async () => {
      for (const field of ['name','category','version','descriptionMarkdown','inputSchema','status']) {
        await reject(`UPDATE scene_submission_extension SET local_fields='{"${field}":"外部数据"}' WHERE id='extra-a'`, '23514');
      }
    });
    await check('同一应用不产生两个未冻结补充草稿', () => reject(`
      INSERT INTO scene_submission_extension(id,application_id,submission_key,created_by_subject)
      VALUES ('extra-duplicate','app-a','publication-duplicate','user-a')`, '23505'));
    const review = `INSERT INTO scene_review_note(id,application_id,submission_extension_id,reviewer_subject,
      recommendation,score_schema,quality_scores,policy_snapshot)`;
    await check('审核前必须冻结补充资料', () => reject(`${review}
      VALUES ('premature','app-a','extra-a','admin','approved',2,'{"formatCompliance":1,"safetyCompliance":1,"annotationCompliance":1,"structuralCompleteness":1,"authenticity":1,"consistency":1,"cleanliness":1}','{}')`, '23514'));
    await check('冻结后可以补登记外部审核编号', async () => {
      await db.exec(`UPDATE scene_submission_extension SET local_fields='{"tutorials":[]}',frozen_at=now() WHERE id='extra-a';
        UPDATE scene_submission_extension SET market_profile='market-test',external_submission_id='S001',external_version_id='MV001'
        WHERE id='extra-a'`);
    });
    await check('冻结后的补充资料不可改写', () => reject(
      "UPDATE scene_submission_extension SET local_fields='{}' WHERE id='extra-a'", '23514'));
    await check('冻结后的外部编号不可调包', () => reject(
      "UPDATE scene_submission_extension SET external_submission_id='S002' WHERE id='extra-a'", '23514'));
    await check('审核建议通过不可漏掉七项评分', () => reject(`${review}
      VALUES ('no-score','app-a','extra-a','admin','approved',2,NULL,'{}')`, '23514'));
    await check('审核评分不能缺项', () => reject(`${review}
      VALUES ('low-score','app-a','extra-a','admin','approved',2,'{}','{}')`, '23514'));
    await check('审核操作与本申请关联，全部一分仍可保存通过结论', async () => {
      await db.exec(`${activity('decision-a','review_decision','live','queued')};
        UPDATE scene_activity SET submission_extension_id='extra-a' WHERE id='decision-a';
        INSERT INTO scene_review_note(id,application_id,submission_extension_id,decision_activity_id,
          reviewer_subject,recommendation,score_schema,quality_scores,policy_snapshot)
        VALUES ('review-a','app-a','extra-a','decision-a','admin','approved',2,'{"formatCompliance":1,"safetyCompliance":1,"annotationCompliance":1,"structuralCompleteness":1,"authenticity":1,"consistency":1,"cleanliness":1}','{}')`);
      assert.equal((await query("SELECT status FROM scene_activity WHERE id='decision-a'"))[0].status, 'queued');
    });
    await check('审核留痕不可修改', () => reject(
      "UPDATE scene_review_note SET reason='覆盖意见' WHERE id='review-a'", '23514'));
    await check('审批操作不能改挂其他应用', () => reject(
      "UPDATE scene_activity SET application_id='app-b' WHERE id='decision-a'", '23514'));
    await check('补材料新建轮次并保留旧资料', async () => {
      await db.exec(`INSERT INTO scene_submission_extension(id,application_id,submission_key,round_no,created_by_subject)
        VALUES ('extra-a2','app-a','publication-a',2,'user-a')`);
      assert.equal((await query("SELECT * FROM scene_submission_extension WHERE application_id='app-a'")).length, 2);
    });
    await check('跨应用的申请关联被外键拒绝', () => reject(`
      ${activity('wrong-extension','publish','live','queued')};
      UPDATE scene_activity SET submission_extension_id='extra-b' WHERE id='wrong-extension'`, '23503'));
    await check('同一请求重复登记被拒绝', async () => {
      await db.exec(activity('run-original','run','live','succeeded'));
      await reject(activity('run-duplicate','run','live','queued','request-run-original'), '23505');
    });
    await check('同一请求编号可以属于不同应用或不同操作', async () => {
      await db.exec(`${activity('run-app-b','run','live','queued','request-run-original','app-b')};
        ${activity('download-same-id','download','live','succeeded','request-run-original')}`);
    });
    await check('请求指纹不可在结果回写时覆盖', () => reject(
      "UPDATE scene_activity SET request_fingerprint=repeat('b',64) WHERE id='run-original'", '23514'));
    await check('未知本地应用的失败同步可以保存', async () => {
      await db.exec(`INSERT INTO scene_activity(id,actor_subject,operation,request_id,request_fingerprint,mode,status,
        provider_profile,workspace_code,agent_code,agent_version_code,completed_at)
        VALUES ('sync-a','user-a','sync','sync-request-a',repeat('c',64),'live','failed','agent-test','ws','A001','001',now())`);
    });
    await check('同步补本地编号后幂等键不变', async () => {
      await db.exec("UPDATE scene_activity SET application_id='app-a' WHERE id='sync-a'");
      await reject(`INSERT INTO scene_activity(id,actor_subject,operation,request_id,request_fingerprint,mode,
        provider_profile,workspace_code,agent_code,agent_version_code)
        VALUES ('sync-dup','user-a','sync','sync-request-a',repeat('c',64),'live','agent-test','ws','A001','001')`, '23505');
    });
    await check('运行不能使用同步专用 unchanged 状态', () => reject(
      activity('run-unchanged','run','live','unchanged'), '23514'));
    await check('收藏、关注和评分按用户隔离', async () => {
      await db.exec(`INSERT INTO scene_interaction(application_id,actor_subject,favorite,favorited_at,follow_updates,followed_at,rating,rated_at)
        VALUES ('app-a','user-a',true,now(),true,now(),5,now()),('app-a','user-b',true,now(),false,NULL,3,now())`);
      await reject("INSERT INTO scene_interaction(application_id,actor_subject) VALUES ('app-a','user-a')", '23505');
    });
    await check('共用活动表时运行/下载/模拟数据不会混算', async () => {
      await db.exec(`${activity('sim-run','run','simulation','succeeded')};
        ${activity('live-failed','run','live','failed')};
        ${activity('live-unknown','run','live','unknown')};
        ${activity('sim-download','download','simulation','succeeded')};
        ${activity('failed-download','download','live','failed')};
        ${activity('extra-download','download','live','succeeded')}`);
      const m = (await query("SELECT * FROM scene_application_metrics WHERE application_id='app-a'"))[0];
      assert.equal(m.favorite_count,2); assert.equal(m.follower_count,1); assert.equal(m.rating_count,2);
      assert.equal(Number(m.rating_average),4); assert.equal(m.download_count,2);
      assert.equal(m.live_usage_count,1); assert.equal(m.simulation_usage_count,1);
      const r = (await query("SELECT * FROM scene_run_metrics WHERE application_id='app-a' AND mode='live'"))[0];
      assert.equal(Number(r.success_rate_percent),50); assert.equal(r.unresolved_count,1);
    });
    await check('没有已完成调用时成功率为空', async () => {
      assert.equal((await query("SELECT success_rate_percent FROM scene_run_metrics WHERE application_id='app-b'"))[0].success_rate_percent,null);
    });
    await check('一条消息对不同管理员分别保存已读', async () => {
      await db.exec(`INSERT INTO scene_notification(id,application_id,actor_subject,event_key,kind,title)
        VALUES ('notice-a','app-a','admin-a','failure:app-a:3','run_failure','失败提醒'),
               ('notice-b','app-a','admin-b','failure:app-a:3','run_failure','失败提醒');
        UPDATE scene_notification SET read_at=now() WHERE id='notice-a' AND actor_subject='admin-a'`);
      const r = await query("SELECT id,read_at FROM scene_notification ORDER BY id");
      assert.ok(r[0].read_at); assert.equal(r[1].read_at,null);
      await reject(`INSERT INTO scene_notification(id,application_id,actor_subject,event_key,kind,title)
        VALUES ('notice-dup','app-a','admin-a','failure:app-a:3','run_failure','重复')`, '23505');
    });
    await check('统一配置的 revision 可检测陈旧更新', async () => {
      const first = await query("UPDATE scene_configuration SET revision=revision+1 WHERE key='form' AND revision=1 RETURNING revision");
      const stale = await query("UPDATE scene_configuration SET revision=revision+1 WHERE key='form' AND revision=1 RETURNING revision");
      assert.equal(first.length,1); assert.equal(stale.length,0);
    });
    await check('外部分类不另存本地字典', () => reject(
      `INSERT INTO scene_configuration(key,value) VALUES ('categories','{"items":["本地分类"]}')`, '23514'));
    await check('配置不能夹带分类副本，热门分类仅保留引用', async () => {
      await reject(`UPDATE scene_configuration SET value=value || '{"categories":["本地分类"]}' WHERE key='workflow'`, '23514');
      await reject(`UPDATE scene_configuration SET value=value || '{"hotCategories":["分类名称"]}' WHERE key='workflow'`, '23514');
      await db.exec(`UPDATE scene_configuration SET value=value || '{"hotCategoryRefs":[{"marketProfile":"market-test","categoryId":"C001"}]}' WHERE key='workflow'`);
    });
    await check('再次执行不覆盖原有表和记录', async () => {
      await assert.rejects(db.exec(sql), e => e.code==='42P07');
      await db.exec('ROLLBACK');
      assert.equal((await query('SELECT * FROM scene_review_note')).length,1);
    });
    const version = (await query('SELECT version() AS version'))[0].version;
    fs.writeFileSync(path.join(__dirname,'validation-result.json'),JSON.stringify({
      checkedAt:new Date().toISOString(), schemaVersion:'2.3', tables:10, engine:version, passed:checks.length,
      schemaSha256:createHash('sha256').update(sql).digest('hex'), checks,
      scope:'独立 PGlite；未连接项目数据库；未验证真实并发、主备或外部接口',
    },null,2)+'\n');
    console.log(`\n${checks.length} checks passed. ${version}`);
  } finally { await db.close(); }
}
main().catch(error => { console.error(error); process.exitCode=1; });
