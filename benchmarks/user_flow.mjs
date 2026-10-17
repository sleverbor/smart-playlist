/** Browser-level acceptance steps. Pass a cua_repl tab; no direct service writes. */
export function userFlow(tab, {name, sourceNames, initial=10, normal=25, checkpoint=async()=>{}, pause=ms=>new Promise(r=>setTimeout(r,ms))}) {
  if (!name?.startsWith('Acceptance test ')) throw Error('Use a dedicated Acceptance test name');
  const p=tab.playwright;
  const report={name, steps:[], pending:['Spotify authorization consent','Early sequential playback','Shuffled/repeated playback','Spotify playlist deletion']};
  const card=()=>p.locator('.mix-card').filter({has:p.getByRole('heading',{name,exact:true})});
  async function until(test,timeout=1800000){const end=Date.now()+timeout;while(Date.now()<end){if(await p.locator('#alert').isVisible()&&(await p.locator('#alert').getAttribute('class'))!=='success'){const error=await p.locator('#alert').textContent();if(error)throw Error(error);}if(await test())return;await pause(700);}throw Error('UI step timed out');}
  async function step(action,fn){const start=Date.now();try{const detail=await fn();report.steps.push({action,status:'passed',seconds:(Date.now()-start)/1000,detail});await checkpoint(action,report);return detail;}catch(e){report.steps.push({action,status:'failed',seconds:(Date.now()-start)/1000,error:String(e)});await checkpoint(action,report);throw e;}}
  async function idle(){await until(async()=>{const text=await p.locator('#alert').textContent().catch(()=> '');if(text&&/limiting requests|cooldown|failed|cannot|error/i.test(text))throw Error(text);return await p.locator('#load-sources').isEnabled();});}
  return {report,
    async connect(){return step('start_authentication',async()=>{await p.getByRole('button',{name:'Connect account',exact:true}).click();await p.getByRole('button',{name:'Start connection',exact:true}).click();report.pending.push('User must approve OAuth; then run create');});},
    async create(){return step('create_and_first_buffer',async()=>{
      await p.getByRole('button',{name:'+ Create a mix',exact:true}).click();
      await p.getByRole('button',{name:'Load playlists',exact:true}).click();await idle();
      for(const source of sourceNames)await p.locator('#source-list label').filter({hasText:source}).getByRole('checkbox').check();
      await p.getByLabel('Playlist name',{exact:true}).fill(name);
      await p.getByLabel('Track spacing',{exact:true}).fill('100');await p.getByLabel('Artist spacing',{exact:true}).fill('20');
      await p.getByLabel('Initial songs',{exact:true}).fill(String(initial));await p.getByLabel('Normal queue size',{exact:true}).fill(String(normal));
      const publishStart=Date.now();await p.getByRole('button',{name:'Create Spotify playlist'}).click();
      await until(async()=>{if(await p.locator('#ready').isVisible())return parseInt(await p.locator('#ready strong').innerText(),10)>=initial;if(await card().count()===0)return false;return (await card().locator('.badge').innerText()).startsWith('Ready')&&parseInt(await card().locator('.mix-stat').innerText(),10)>=initial;});
      return {initial,normal,click_to_ready_seconds:(Date.now()-publishStart)/1000,note:'Now start early playback; setup may still be importing.'};
    });},
    async finishSetup(){return step('finish_setup',async()=>{await idle();if(await card().getByRole('button',{name:'Continue setup',exact:true}).count()){await card().getByRole('button',{name:'Continue setup',exact:true}).click();await idle();}const text=await card().innerText();if(parseInt(await card().locator('.mix-stat').innerText(),10)!==normal)throw Error('Expected configured buffer size');return text;});},
    async sync(label='sync'){return step(label,async()=>{await idle();if(await p.locator('#preview').isVisible())await p.locator('#close-preview').click();await card().getByRole('button',{name:'Preview sync',exact:true}).click();await until(()=>p.locator('#preview').isVisible());const summary=await p.locator('#preview-summary').innerText();await p.locator('#apply').click();await p.locator('#confirm-apply').click();await idle();return {summary,queue:await card().innerText()};});},
    async removeSetup(){return step('remove_setup',async()=>{await idle();await card().getByRole('button',{name:'Remove setup',exact:true}).click();await p.locator('#confirm-apply').click();await until(async()=>await card().count()===0);return 'Dedicated test setup removed';});}
  };
}
