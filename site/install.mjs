const agentIds = {codex: 'codex', claude: 'claude-code', cursor: 'cursor', antigravity: 'antigravity', gemini: 'gemini-cli'};

export function installation(skills, agents, repository, scope = 'project') {
  if (!/^[a-zA-Z0-9_.-]+\/[a-zA-Z0-9_.-]+$/.test(repository)) throw Error('Invalid repository');
  if (!skills.length || skills.some(skill => !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(skill.id))) throw Error('Invalid skills');
  if (!agents.length || agents.some(agent => !Object.hasOwn(agentIds, agent))) throw Error('Unknown agent');
  if (!['project', 'global'].includes(scope)) throw Error('Invalid scope');
  return format(skills.map(skill => skill.id), agents.map(agent => agentIds[agent]), repository, scope);
}

// Unvalidated: also draws the terminal preview, whose placeholders are not real arguments.
function format(skillIds, agentNames, repository, scope) {
  const parts = [`DISABLE_TELEMETRY=1 npx skills add ${repository}`, `--skill ${skillIds.join(' ')}`, `--agent ${agentNames.join(' ')}`];
  if (scope === 'global') parts.push('--global');
  return parts.join(' \\\n  ');
}

export function usageExample(skill, agent) {
  return agent === 'codex' ? skill.usage : skill.example;
}

// The end-to-end tutorial casts Claude as orchestrator, so it needs the whole bundle and Claude.
export function bundleExample(bundle, ids, agents) {
  const bundleIds = bundle?.steps.flatMap(step => step.skills) ?? [];
  const complete = bundleIds.length > 0 && bundleIds.every(id => ids.includes(id));
  return complete && agents.includes('claude') ? bundle.example ?? null : null;
}

if (typeof document !== 'undefined') {
  const byId = id => document.getElementById(id);
  let catalog;
  let resetCopy;

  // Colours a command token by token; textContent, and so what Copy copies, stays the command.
  function paint(target, command) {
    let flag = '';
    target.replaceChildren(...command.split(/(\s+)/).map(token => {
      if (!token.trim()) return token;
      const kind = /^[A-Z_]+=/.test(token) ? 'env' : token.startsWith('--') ? 'flag' : token === '\\' ? 'cont'
        : token.startsWith('<') ? 'ph' : token.includes('/') ? 'repo' : flag || 'cmd';
      if (kind === 'flag') flag = {'--skill': 'skill', '--agent': 'agent'}[token] ?? '';
      const span = document.createElement('span'); span.className = `tok-${kind}`; span.textContent = token;
      return span;
    }));
  }

  function render() {
    clearTimeout(resetCopy);
    byId('copy').textContent = 'Copy command';
    byId('copy').removeAttribute('data-copied');
    const ids = [...document.querySelectorAll('input[name="skill"]:checked')].map(input => input.value);
    const skills = catalog.skills.filter(skill => ids.includes(skill.id));
    const agents = [...document.querySelectorAll('input[name="agent"]:checked')].map(input => input.value);
    const scope = document.querySelector('input[name="scope"]:checked').value;
    const ready = skills.length > 0 && agents.length > 0;
    byId('install-panel').dataset.state = ready ? 'ready' : 'empty';
    byId('copy').disabled = !ready;
    byId('install-content').hidden = !ready;
    byId('install-help').hidden = !ready;
    byId('your-turn-link').hidden = !ids.includes('your-turn-handoff');
    byId('status').textContent = '';

    let selected = false;
    if (catalog.bundle) {
      const bundle = catalog.bundle.steps.flatMap(step => step.skills);
      selected = bundle.length === ids.length && bundle.every(id => ids.includes(id));
      byId('flow-select').setAttribute('aria-pressed', selected);
      byId('flow-select').textContent = selected ? 'Bundle selected' : `Select ${bundle.length} skills`;
      // Light up the loop diagram with what the checkboxes have armed.
      const armed = bundle.filter(id => ids.includes(id)).length;
      const closed = armed === bundle.length;
      for (const chip of document.querySelectorAll('.flow-chip')) chip.setAttribute('aria-pressed', ids.includes(chip.dataset.skill));
      for (const item of byId('flow-steps').children) {
        const own = item.dataset.skills ? item.dataset.skills.split(' ') : [];
        const on = own.filter(id => ids.includes(id)).length;
        const state = own.length ? (on === own.length ? 'armed' : on ? 'partial' : 'idle') : closed ? 'armed' : 'idle';
        item.dataset.state = state;
        item.querySelector('.step-state').textContent = state === 'armed' && !own.length ? 'loop closed' : state;
      }
      byId('flow').dataset.loop = closed ? 'closed' : 'open';
      byId('flow-armed').textContent = `${armed}/${bundle.length} armed`;
      byId('flow-armed').style.setProperty('--p', armed / bundle.length);
    }

    if (ready) paint(byId('commands'), installation(skills, agents, catalog.repository, scope));
    else byId('commands').replaceChildren();
    byId('preview').hidden = ready;
    if (!ready) {
      paint(byId('preview'), format(ids.length ? ids : ['<skills>'], agents.length ? agents.map(agent => agentIds[agent]) : ['<agents>'], catalog.repository, scope));
    }
    byId('preview-load').hidden = ready || !catalog.bundle || selected;
    byId('term-path').textContent = scope === 'project' ? '~/your-project' : '~';
    byId('where').textContent = ready
      ? `Paste in ${scope === 'project' ? 'your project folder' : 'your terminal'}.`
      : !skills.length ? 'Choose one or more skills to get your install command.' : 'Choose the agents you use to get your install command.';

    byId('examples').replaceChildren();
    const tutorial = bundleExample(catalog.bundle, ids, agents);
    if (tutorial) {
      const prompt = document.createElement('pre'); prompt.tabIndex = 0; prompt.textContent = tutorial.prompt;
      const steps = document.createElement('ol'); steps.className = 'example-steps';
      for (const step of tutorial.steps) {
        const item = document.createElement('li'); const name = document.createElement('code'); name.textContent = step.skill;
        item.append(name, ` ${step.text}`); steps.append(item);
      }
      const value = document.createElement('p'); value.className = 'help'; value.textContent = tutorial.value;
      byId('examples').append(prompt, steps, value);
    } else for (const skill of skills) {
      const example = document.createElement('pre'); example.tabIndex = 0;
      example.textContent = usageExample(skill, agents.length === 1 ? agents[0] : '');
      byId('examples').append(example);
    }
  }

  document.querySelector('.choices').addEventListener('change', () => { if (catalog) render(); });
  byId('copy').addEventListener('click', async () => {
    const command = byId('commands').textContent;
    try {
      await navigator.clipboard.writeText(command);
      if (byId('commands').textContent !== command) return;
      clearTimeout(resetCopy);
      byId('copy').textContent = '✓ Copied to clipboard';
      byId('copy').dataset.copied = '';
      resetCopy = setTimeout(() => { byId('copy').textContent = 'Copy command'; byId('copy').removeAttribute('data-copied'); }, 1800);
      byId('status').textContent = 'Command copied. Paste it in your terminal.';
    } catch {
      if (byId('commands').textContent !== command) return;
      clearTimeout(resetCopy);
      byId('copy').textContent = 'Copy manually';
      const selection = getSelection(); const range = document.createRange();
      range.selectNodeContents(byId('commands'));
      selection.removeAllRanges(); selection.addRange(range);
      byId('status').textContent = 'Clipboard unavailable. Copy the selected command manually.';
    }
  });

  fetch('catalog.json').then(response => { if (!response.ok) throw Error('Catalog unavailable'); return response.json(); }).then(data => {
    if (!data.skills?.length) throw Error('Empty catalog');
    // Validate every catalogue value before it can enter a shell command.
    installation(data.skills, Object.keys(agentIds), data.repository);
    catalog = data; byId('skills').replaceChildren();
    for (const skill of data.skills) {
      const label = document.createElement('label'); const input = document.createElement('input');
      input.type = 'checkbox'; input.name = 'skill'; input.value = skill.id;
      // First visit arms the whole loop, so Copy works in one click.
      input.checked = data.bundle?.steps.some(step => step.skills.includes(skill.id)) ?? false;
      const text = document.createElement('span');
      const name = document.createElement('strong'); name.textContent = skill.label;
      label.title = `${skill.label} v${skill.version}`;
      const description = document.createElement('small'); description.textContent = skill.description;
      text.append(name, description); label.append(input, text); byId('skills').append(label);
    }
    if (data.bundle) {
      byId('flow-title').textContent = data.bundle.label;
      byId('flow-description').textContent = data.bundle.description;
      data.bundle.steps.forEach((step, index) => {
        const item = document.createElement('li'); item.dataset.skills = step.skills.join(' ');
        const head = document.createElement('span'); head.className = 'step-head';
        const number = document.createElement('span'); number.className = 'field-step'; number.textContent = String(index + 1).padStart(2, '0');
        const state = document.createElement('span'); state.className = 'step-state'; head.append(number, state);
        const title = document.createElement('strong'); title.textContent = step.title;
        const text = document.createElement('small'); text.textContent = step.text;
        if (step.skills.length) {
          const chips = document.createElement('span'); chips.className = 'flow-chips';
          for (const id of step.skills) {
            const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'flow-chip';
            chip.dataset.skill = id; chip.textContent = id; chip.title = `Toggle ${id}`;
            chip.addEventListener('click', () => {
              const input = [...document.querySelectorAll('input[name="skill"]')].find(box => box.value === id);
              if (input) { input.checked = !input.checked; render(); }
            });
            chips.append(chip);
          }
          item.append(head, title, text, chips);
        } else {
          const copy = document.createElement('div'); copy.className = 'closing-copy';
          copy.append(title, text);
          item.append(head, copy);
        }
        byId('flow-steps').append(item);
      });
      byId('flow-select').addEventListener('click', () => {
        const bundle = data.bundle.steps.flatMap(step => step.skills);
        for (const input of document.querySelectorAll('input[name="skill"]')) input.checked = bundle.includes(input.value);
        render();
        byId('skills').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'nearest'});
      });
      byId('preview-load').addEventListener('click', () => byId('flow-select').click());
      byId('flow').hidden = false;
    }
    render();
  }).catch(() => {
    catalog = undefined; byId('skills').textContent = 'Skill list unavailable.';
    byId('install-panel').dataset.state = 'error';
    byId('where').textContent = 'Could not load the skill list. Reload this page to try again.';
    byId('copy').disabled = true; byId('install-content').hidden = true; byId('install-help').hidden = true;
    byId('preview').hidden = true; byId('preview-load').hidden = true;
  });
}
