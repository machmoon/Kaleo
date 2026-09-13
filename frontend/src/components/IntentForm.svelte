<script>
  import MicButton from './MicButton.svelte'
  import { MAX_ENCLOSURE_STYLE_CHARS, MAX_REQUEST_BYTES, MAX_TIME_LIMIT_S, MIN_TIME_LIMIT_S, normalizeRequest, requestBytes } from '../lib/api.js'
  import { appendTranscript } from '../lib/voice.js'
  import {
    constraintFieldExample,
    constraintManifestReady,
    newConstraintClass,
    normalizeConstraintManifest,
    suggestConstraintManifest,
  } from '../lib/constraints.js'
  import { DATASHEET_PRESETS } from '../lib/datasheets.js'

  let {
    onsubmit,
    initial = null,
    models = [],
    initialModel = 'gemini-3.7-flash',
    initialThinkingLevel = 'auto',
    initialQuotaRpm = 'auto',
    placementCapabilities = {},
  } = $props()

  const ORCHESTRATOR_MODELS = [
    {
      id: 'gemini-3.7-flash',
      name: 'Gemini 3.7 Flash',
      note: 'Stable · strongest current agent model',
    },
    {
      id: 'gemini-3.1-pro-preview',
      name: 'Gemini 3.1 Pro',
      note: 'Preview · deeper reasoning by default',
    },
  ]

  // Seeded once, on purpose: App remounts this form whenever the phase returns
  // to idle, so the fields stay editable rather than tracking the prop.
  // svelte-ignore state_referenced_locally
  const seed = initial || {}
  const seedSheets = Object.entries(seed.datasheets || {})
  const hasSeedConstraints = Boolean(
    seed.constraints && typeof seed.constraints === 'object' && !Array.isArray(seed.constraints),
  )
  const seedConstraints = hasSeedConstraints
    ? normalizeConstraintManifest(seed.constraints)
    : suggestConstraintManifest(seed.intent ?? '')
  // Restoring a session or starting another board creates a new run. Require a
  // fresh approval even when the saved v2 manifest itself was well formed.
  seedConstraints.approved = false

  let intent = $state(seed.intent ?? '')
  let constraintsEnabled = $state(hasSeedConstraints)
  let constraintsStarted = $state(hasSeedConstraints)
  let showConstraints = $state(hasSeedConstraints)
  let constraints = $state(seedConstraints)
  let timeLimit = $state(seed.time_limit_s ?? 20)
  let noSolverBudget = $state(seed.no_solver_budget !== false)
  let review = $state(seed.review !== false)
  let ground = $state(seed.ground === true)
  // Demo-first default: a fresh form ships with the case on. A restored or
  // edited request keeps whatever it actually said — an explicit false (or a
  // normalized request that omitted the key, which is how "off" is sent) stays
  // off rather than being silently re-enabled.
  // svelte-ignore state_referenced_locally
  let enclosure = $state(initial ? seed.enclosure === true : true)
  let enclosureRigorous = $state(seed.enclosure_rigorous === true)
  let enclosureStyle = $state(seed.enclosure_style ?? '')
  // Off by default: sourcing is one more model call plus a network probe per
  // part, and unlike the case it has no demo payoff worth spending that on
  // unasked. A restored request keeps what it said.
  let sourcing = $state(seed.sourcing === true)
  // Seeded once with the rest of the editable form state.
  // svelte-ignore state_referenced_locally
  let orchestratorModel = $state(initialModel || 'gemini-3.7-flash')
  // svelte-ignore state_referenced_locally
  let thinkingLevel = $state(initialThinkingLevel || 'auto')
  // svelte-ignore state_referenced_locally
  let quotaRpm = $state(String(initialQuotaRpm || 'auto'))
  let placementEnabled = $state(seed.placement_enabled !== false)
  let placementProfile = $state(seed.placement_profile || 'compact-control')
  let placementPolicy = $state(seed.placement_policy || 'deterministic')
  let experimentalPlacement = $state(seed.experimental_placement === true)
  let recordTrace = $state(seed.record_trace === true)
  let showDatasheets = $state(seedSheets.length > 0)
  let rows = $state(
    seedSheets.length ? seedSheets.map(([part, url]) => ({ part, url })) : [{ part: '', url: '' }],
  )

  let textarea = $state(null)

  // Grounding retrieves against datasheet pages, so with no datasheet it has
  // nothing to ground on and is never sent, however the box was left.
  const hasDatasheets = $derived(
    rows.some((r) => String(r.part ?? '').trim() && String(r.url ?? '').trim()),
  )

  const request = $derived({
    intent,
    ...(constraintsEnabled ? { constraints } : {}),
    datasheets: Object.fromEntries(rows.map((r) => [r.part, r.url])),
    time_limit_s: timeLimit,
    no_solver_budget: noSolverBudget,
    review,
    ground: ground && hasDatasheets,
    enclosure,
    enclosure_rigorous: enclosureRigorous,
    enclosure_style: enclosureStyle,
    sourcing,
    placement_enabled: placementEnabled,
    placement_profile: placementProfile,
    placement_policy: placementPolicy,
    experimental_placement: experimentalPlacement,
    record_trace: experimentalPlacement && recordTrace,
  })

  // The service rejects oversize bodies with 413; blocking here keeps the user
  // from waiting on a round trip that can only fail.
  const bytes = $derived(requestBytes(normalizeRequest(request)))
  const tooLarge = $derived(bytes > MAX_REQUEST_BYTES)
  const advertisedModels = $derived(new Set(models.map((model) => String(model?.id ?? ''))))
  const legacyModels = $derived(new Set(models.filter((model) => model?.legacy).map((model) => String(model.id))))
  const selectedUnavailable = $derived(
    advertisedModels.size > 0 && !advertisedModels.has(orchestratorModel),
  )
  const constraintsReady = $derived(constraintManifestReady(constraints))
  const canSubmit = $derived(
    intent.trim().length > 0
      && !tooLarge
      && !selectedUnavailable
      && (!constraintsEnabled || (constraintsReady && constraints.approved === true)),
  )
  const placementPolicies = $derived(placementCapabilities?.policies || {})

  function grow() {
    if (!textarea) return
    textarea.style.height = 'auto'
    textarea.style.height = `${textarea.scrollHeight}px`
  }

  function invalidateConstraintApproval() {
    if (constraints.approved) constraints.approved = false
  }

  function commaValues(value) {
    return String(value || '').split(',').map((item) => item.trim()).filter(Boolean)
  }

  function toggleConstraints(event) {
    constraintsEnabled = event.currentTarget.checked
    if (constraintsEnabled && !constraintsStarted) {
      constraints = suggestConstraintManifest(intent)
      constraintsStarted = true
    }
    constraints.approved = false
    if (constraintsEnabled) showConstraints = true
  }

  function resetConstraintSuggestions() {
    constraints = suggestConstraintManifest(intent)
    constraintsStarted = true
  }

  function addConstraintClass() {
    constraints.net_classes = [...constraints.net_classes, newConstraintClass('signal')]
    invalidateConstraintApproval()
  }

  function removeConstraintClass(index) {
    constraints.net_classes = constraints.net_classes.filter((_, itemIndex) => itemIndex !== index)
    invalidateConstraintApproval()
  }

  function addKeepout() {
    constraints.mechanical.keepouts = [
      ...constraints.mechanical.keepouts,
      { name: '', x_mm: null, y_mm: null, width_mm: null, height_mm: null },
    ]
    invalidateConstraintApproval()
  }

  function removeKeepout(index) {
    constraints.mechanical.keepouts = constraints.mechanical.keepouts.filter((_, itemIndex) => itemIndex !== index)
    invalidateConstraintApproval()
  }

  function addFixedPlacement() {
    constraints.mechanical.fixed_placements = [
      ...constraints.mechanical.fixed_placements,
      { ref: '', x_mm: null, y_mm: null, tolerance_mm: null },
    ]
    invalidateConstraintApproval()
  }

  function removeFixedPlacement(index) {
    constraints.mechanical.fixed_placements = constraints.mechanical.fixed_placements.filter((_, itemIndex) => itemIndex !== index)
    invalidateConstraintApproval()
  }

  function addThermalPair(netClass) {
    netClass.thermal_pairs = [...netClass.thermal_pairs, ['', '']]
    invalidateConstraintApproval()
  }

  function removeThermalPair(netClass, index) {
    netClass.thermal_pairs = netClass.thermal_pairs.filter((_, itemIndex) => itemIndex !== index)
    invalidateConstraintApproval()
  }

  $effect(() => {
    intent
    grow()
  })

  function addRow() {
    rows = [...rows, { part: '', url: '' }]
  }

  function useDatasheet(preset) {
    const next = rows.map((row) => ({ ...row }))
    const existing = next.findIndex((row) => String(row.part ?? '').trim() === preset.part)
    const blank = next.findIndex(
      (row) => !String(row.part ?? '').trim() && !String(row.url ?? '').trim(),
    )

    if (existing >= 0) next[existing] = { part: preset.part, url: preset.url }
    else if (blank >= 0) next[blank] = { part: preset.part, url: preset.url }
    else next.push({ part: preset.part, url: preset.url })

    rows = next
    showDatasheets = true
  }

  function submit(event) {
    event.preventDefault()
    if (!canSubmit) return
    onsubmit(request, { model: orchestratorModel, thinkingLevel, quotaRpm })
  }

  function toggleExperimental() {
    experimentalPlacement = !experimentalPlacement
    if (!experimentalPlacement) {
      recordTrace = false
      if (['fast', 'ollama', 'tinker', 'hybrid'].includes(placementPolicy)) {
        placementPolicy = 'deterministic'
      }
    }
  }
</script>

<form class="form" data-testid="intent-form" onsubmit={submit}>
  <label class="lbl" for="intent">What do you want on the board</label>
  <textarea
    id="intent"
    bind:this={textarea}
    bind:value={intent}
    oninput={invalidateConstraintApproval}
    rows="3"
    placeholder="an STM32F030 board with a 3.3 V regulator and a motor driver"
    data-testid="intent-form-intent"
    data-material="panel"
  ></textarea>

  <div class="counter" class:over={tooLarge} data-testid="intent-form-counter">
    {#if tooLarge}
      {bytes.toLocaleString()} bytes — over the {MAX_REQUEST_BYTES.toLocaleString()} byte limit
    {:else}
      {intent.length.toLocaleString()} characters
    {/if}
  </div>

  <details class="constraints" bind:open={showConstraints} data-testid="intent-form-constraints" data-material="panel">
    <summary>
      <span class="lbl">verified constraints</span>
      <span class="summary-note">optional · exact nets and measurable limits</span>
    </summary>
    <div class="constraint-intro">
      <div>
        <strong>Engineer-approved contract</strong>
        <p>Examples appear only as placeholders. They are never copied into the request for you.</p>
      </div>
      <label class="constraint-switch">
        <input type="checkbox" checked={constraintsEnabled} onchange={toggleConstraints} data-testid="intent-form-constraints-enabled" />
        <span>Enable for this run</span>
      </label>
    </div>

    {#if constraintsEnabled}
      <div class="constraint-actions">
        <span>Only the current two-layer engine is available: F.Cu and B.Cu.</span>
        <button type="button" onclick={resetConstraintSuggestions}>Reset suggestions from prompt</button>
      </div>

      <label class="constraint-field board-layers">
        <span>Board layers</span>
        <input type="number" min="2" max="2" bind:value={constraints.board_layers} oninput={invalidateConstraintApproval} />
        <small>The current engine supports exactly 2.</small>
      </label>

      {#each constraints.net_classes as netClass, classIndex (classIndex)}
        <article class="net-class" data-testid="intent-form-constraint-class">
          <div class="net-class-head">
            <strong>Net class {classIndex + 1}</strong>
            <button type="button" class="delete" onclick={() => removeConstraintClass(classIndex)}>Delete class</button>
          </div>
          <div class="constraint-grid">
            <label class="constraint-field"><span>Name</span><input bind:value={netClass.name} oninput={invalidateConstraintApproval} placeholder="Critical signals" /></label>
            <label class="constraint-field">
              <span>Kind</span>
              <select bind:value={netClass.kind} onchange={invalidateConstraintApproval}>
                {#each ['signal', 'i2c', 'usb', 'ethernet', 'spi', 'clock', 'power', 'analog', 'rf'] as kind}
                  <option value={kind}>{kind}</option>
                {/each}
              </select>
            </label>
            <label class="constraint-field span-two">
              <span>Exact net names</span>
              <input value={netClass.nets.join(', ')} placeholder={`Example: ${constraintFieldExample(netClass.kind, 'nets')}`} oninput={(event) => { netClass.nets = commaValues(event.currentTarget.value); invalidateConstraintApproval() }} />
            </label>
            <label class="constraint-field span-two">
              <span>Allowed layers</span>
              <input value={netClass.allowed_layers.join(', ')} placeholder={`Example: ${constraintFieldExample(netClass.kind, 'allowed_layers')}`} oninput={(event) => { netClass.allowed_layers = commaValues(event.currentTarget.value); invalidateConstraintApproval() }} />
            </label>
            <label class="constraint-field"><span>Maximum layer transitions</span><input type="number" min="0" bind:value={netClass.max_layer_transitions} placeholder={constraintFieldExample(netClass.kind, 'max_layer_transitions')} oninput={invalidateConstraintApproval} /></label>
            <label class="constraint-field"><span>Maximum vias per net</span><input type="number" min="0" bind:value={netClass.max_vias_per_net} placeholder={constraintFieldExample(netClass.kind, 'max_vias_per_net')} oninput={invalidateConstraintApproval} /></label>
          </div>

          <details class="constraint-detail">
            <summary>Electrical and routed-copper limits</summary>
            <div class="constraint-grid limit-grid">
              {#each [
                ['signal_voltage_v', 'Signal voltage (V)'], ['max_frequency_hz', 'Maximum frequency (Hz)'],
                ['min_trace_width_mm', 'Minimum trace width (mm)'], ['max_length_mm', 'Maximum length (mm)'],
                ['max_skew_mm', 'Maximum skew (mm)'], ['max_stub_length_mm', 'Maximum stub length (mm)'],
                ['expected_current_a', 'Expected current (A)'], ['copper_weight_oz', 'Copper weight (oz)'],
                ['max_voltage_drop_v', 'Maximum voltage drop (V)'], ['min_separation_mm', 'Minimum isolation (mm)'],
                ['min_thermal_separation_mm', 'Minimum thermal separation (mm)'],
              ] as [field, label] (field)}
                <label class="constraint-field"><span>{label}</span><input type="number" min="0" step="any" bind:value={netClass[field]} placeholder={constraintFieldExample(netClass.kind, field)} oninput={invalidateConstraintApproval} /></label>
              {/each}
              <label class="constraint-field"><span>Reference plane</span><input bind:value={netClass.reference_plane} placeholder={`Example: ${constraintFieldExample(netClass.kind, 'reference_plane')}`} oninput={invalidateConstraintApproval} /></label>
              <label class="constraint-check"><input type="checkbox" bind:checked={netClass.guard_required} onchange={invalidateConstraintApproval} /><span>Guard required</span></label>
              <label class="constraint-check"><input type="checkbox" bind:checked={netClass.pullups_required} onchange={invalidateConstraintApproval} /><span>Pull-ups required</span></label>
              <label class="constraint-check"><input type="checkbox" bind:checked={netClass.controlled_impedance} onchange={invalidateConstraintApproval} /><span>Controlled impedance required</span></label>
              {#if netClass.pullups_required}
                <label class="constraint-field"><span>Pull-up rail</span><input bind:value={netClass.pullup_rail} placeholder={constraintFieldExample(netClass.kind, 'pullup_rail')} oninput={invalidateConstraintApproval} /></label>
                {#each [['pullup_min_ohms', 'Pull-up minimum (Ω)'], ['pullup_max_ohms', 'Pull-up maximum (Ω)'], ['bus_capacitance_pf', 'Bus capacitance (pF)'], ['max_rise_time_ns', 'Maximum rise time (ns)']] as [field, label] (field)}
                  <label class="constraint-field"><span>{label}</span><input type="number" min="0" step="any" bind:value={netClass[field]} placeholder={constraintFieldExample(netClass.kind, field)} oninput={invalidateConstraintApproval} /></label>
                {/each}
              {/if}
              {#if netClass.controlled_impedance}
                {#each [['impedance_ohms', 'Target impedance (Ω)'], ['impedance_tolerance_percent', 'Tolerance (%)'], ['pair_spacing_mm', 'Pair spacing (mm)']] as [field, label] (field)}
                  <label class="constraint-field"><span>{label}</span><input type="number" min="0" step="any" bind:value={netClass[field]} placeholder={constraintFieldExample(netClass.kind, field)} oninput={invalidateConstraintApproval} /></label>
                {/each}
              {/if}
              <label class="constraint-field span-two"><span>Review concerns</span><input value={netClass.concerns.join(', ')} oninput={(event) => { netClass.concerns = commaValues(event.currentTarget.value); invalidateConstraintApproval() }} /></label>
            </div>
            <h4>Thermal reference pairs</h4>
            <p class="detail-note">Name both exact references for each thermal-separation check in this net class.</p>
            {#each netClass.thermal_pairs as pair, index (index)}
              <div class="mechanical-row pair-row">
                <input bind:value={pair[0]} placeholder="First ref" aria-label="Thermal pair first reference" oninput={invalidateConstraintApproval} />
                <input bind:value={pair[1]} placeholder="Second ref" aria-label="Thermal pair second reference" oninput={invalidateConstraintApproval} />
                <button type="button" class="delete" onclick={() => removeThermalPair(netClass, index)}>Delete</button>
              </div>
            {/each}
            <button type="button" class="add" onclick={() => addThermalPair(netClass)}>Add thermal pair</button>
          </details>
        </article>
      {/each}
      <button type="button" class="add constraint-add" onclick={addConstraintClass}>Add net class</button>

      <details class="constraint-detail mechanical">
        <summary>Mechanical hard constraints</summary>
        <div class="constraint-grid">
          {#each [['max_board_width_mm', 'Maximum board width (mm)'], ['max_board_height_mm', 'Maximum board height (mm)'], ['max_component_height_mm', 'Maximum component height (mm)']] as [field, label] (field)}
            <label class="constraint-field"><span>{label}</span><input type="number" min="0.01" step="any" bind:value={constraints.mechanical[field]} oninput={invalidateConstraintApproval} /></label>
          {/each}
          <label class="constraint-field"><span>Mounting-hole refs</span><input value={constraints.mechanical.mounting_hole_refs.join(', ')} placeholder="Example: H1, H2" oninput={(event) => { constraints.mechanical.mounting_hole_refs = commaValues(event.currentTarget.value); invalidateConstraintApproval() }} /></label>
        </div>
        <h4>Component keepouts</h4>
        {#each constraints.mechanical.keepouts as keepout, index (index)}
          <div class="mechanical-row keepout-row">
            <input bind:value={keepout.name} placeholder="Name" aria-label="Keepout name" oninput={invalidateConstraintApproval} />
            {#each [['x_mm', 'X'], ['y_mm', 'Y'], ['width_mm', 'Width'], ['height_mm', 'Height']] as [field, label] (field)}
              <input type="number" min="0" step="any" bind:value={keepout[field]} aria-label={`Keepout ${label}`} placeholder={`${label} mm`} oninput={invalidateConstraintApproval} />
            {/each}
            <button type="button" class="delete" onclick={() => removeKeepout(index)}>Delete</button>
          </div>
        {/each}
        <button type="button" class="add" onclick={addKeepout}>Add keepout</button>

        <h4>Fixed placements</h4>
        {#each constraints.mechanical.fixed_placements as fixed, index (index)}
          <div class="mechanical-row fixed-row">
            <input bind:value={fixed.ref} placeholder="Reference" aria-label="Fixed reference" oninput={invalidateConstraintApproval} />
            {#each [['x_mm', 'X', 0], ['y_mm', 'Y', 0], ['tolerance_mm', 'Tolerance', 0.001]] as [field, label, minimum] (field)}
              <input type="number" min={minimum} step="any" bind:value={fixed[field]} aria-label={`Fixed ${label}`} placeholder={`${label} mm`} oninput={invalidateConstraintApproval} />
            {/each}
            <button type="button" class="delete" onclick={() => removeFixedPlacement(index)}>Delete</button>
          </div>
        {/each}
        <button type="button" class="add" onclick={addFixedPlacement}>Add fixed part</button>

      </details>

      <details class="constraint-detail">
        <summary>Soft scoring weights</summary>
        <p class="detail-note">Zero disables a preference. These values score the result; they do not override a hard blocker.</p>
        <div class="constraint-grid">
          {#each Object.entries(constraints.soft_preferences) as [field, value] (field)}
            <label class="constraint-field"><span>{field.replaceAll('_', ' ')}</span><input type="number" min="0" step="0.1" value={value} oninput={(event) => { constraints.soft_preferences[field] = Number(event.currentTarget.value); invalidateConstraintApproval() }} /></label>
          {/each}
        </div>
      </details>

      <label class="approval">
        <input type="checkbox" bind:checked={constraints.approved} disabled={!constraintsReady} data-testid="intent-form-constraints-approved" />
        <span>I approve these exact names and limits. Unresolved hard checks may block promotion.</span>
      </label>
      {#if !constraintsReady}
        <p class="constraint-warning" role="status">Complete every required exact name and measurable value before approval.</p>
      {:else if !constraints.approved}
        <p class="constraint-warning" role="status">Review and approve the contract. Editing the prompt or contract clears approval.</p>
      {/if}
    {/if}
  </details>

  <details class="sheets" bind:open={showDatasheets} data-testid="intent-form-datasheets">
    <summary class="lbl" data-testid="intent-form-datasheets-summary">with datasheets</summary>
    <div class="presets" data-testid="intent-form-datasheet-presets">
      <div class="preset-heading lbl">available to use</div>
      {#each DATASHEET_PRESETS as preset (preset.part)}
        <div class="preset" data-testid="intent-form-datasheet-preset" data-material="panel">
          <div class="preset-copy">
            <span class="mono preset-part">{preset.part}</span>
            <span class="preset-maker">{preset.manufacturer}</span>
          </div>
          <a class="pdf" href={preset.url} target="_blank" rel="noreferrer">View PDF</a>
          <button
            type="button"
            class="use"
            data-testid="intent-form-use-datasheet"
            disabled={rows.some(
              (row) => String(row.part ?? '').trim() === preset.part
                && String(row.url ?? '').trim() === preset.url,
            )}
            onclick={() => useDatasheet(preset)}
          >
            {rows.some(
              (row) => String(row.part ?? '').trim() === preset.part
                && String(row.url ?? '').trim() === preset.url,
            ) ? 'Selected' : 'Use datasheet'}
          </button>
        </div>
      {/each}
    </div>
    <div class="sheet-rows">
      {#each rows as row, i (i)}
        <div class="sheet-row" data-testid="intent-form-datasheet-row">
          <input class="mono part" bind:value={row.part} placeholder="U1" aria-label="Part reference" data-testid="intent-form-datasheet-part" data-material="panel" />
          <input class="mono url" bind:value={row.url} placeholder="https://…/STM32F030C8.pdf" aria-label="Datasheet URL" data-testid="intent-form-datasheet-url" data-material="panel" />
        </div>
      {/each}
      <button type="button" class="add" data-testid="intent-form-add-datasheet" onclick={addRow}>Add another datasheet</button>
    </div>
  </details>

  <!-- Power-user controls whose defaults are already correct: closed by
       default so the first screen reads intent → run, like the datasheets
       disclosure above. -->
  <details class="orchestrator" data-testid="intent-form-orchestrator" data-material="panel">
    <summary class="lbl" data-testid="intent-form-orchestrator-summary">orchestrator · advanced</summary>
    <div class="orchestrator-controls">
      <label class="orchestrator-control">
        <span>Model</span>
        <select bind:value={orchestratorModel} data-testid="intent-form-orchestrator-model" data-material="tint">
          {#each ORCHESTRATOR_MODELS as option (option.id)}
            <option
              value={option.id}
              disabled={(advertisedModels.size > 0 && !advertisedModels.has(option.id)) || legacyModels.has(option.id)}
            >
              {option.name}{advertisedModels.size > 0 && !advertisedModels.has(option.id) ? ' · unavailable' : legacyModels.has(option.id) ? ' · below the Gemini 3.5 floor' : ''}
            </option>
          {/each}
        </select>
        <small>{ORCHESTRATOR_MODELS.find((option) => option.id === orchestratorModel)?.note}</small>
      </label>

      <label class="orchestrator-control">
        <span>Reasoning effort</span>
        <select bind:value={thinkingLevel} data-testid="intent-form-thinking-level" data-material="tint">
          <option value="auto">Auto · model default</option>
          <option value="low">Fast · low</option>
          <option value="medium">Standard · medium</option>
          <option value="high">Deep · high</option>
        </select>
        <small>Gemini 3 always thinks internally; Fast uses its lowest supported effort.</small>
      </label>

      <label class="orchestrator-control">
        <span>Request pace</span>
        <select bind:value={quotaRpm} data-testid="intent-form-quota-rpm" data-material="tint">
          <option value="auto">Auto · no app limit</option>
          <option value="15">Fast · 15 RPM</option>
          <option value="6">Demo-safe · 6 RPM</option>
          <option value="3">Conservative · 3 RPM</option>
        </select>
        <small>Spaces Gemini calls across this service instance. It cannot increase daily or token quota.</small>
      </label>
    </div>
  </details>
  <!-- Outside the disclosure: the submit button is disabled by this, so the
       reason must be visible even while the panel is closed. -->
  {#if selectedUnavailable}
    <p class="model-warning" role="status">This model is not advertised by the configured API key.</p>
  {/if}

  <section class="placement-settings" data-testid="intent-form-placement" data-material="panel">
    <div class="placement-heading">
      <div>
        <span class="lbl">verified placement</span>
        <p>Run the generated board through deterministic geometry before routing.</p>
      </div>
      <label class="placement-on">
        <input type="checkbox" bind:checked={placementEnabled} data-testid="intent-form-placement-enabled" />
        <span>Run placement repair</span>
      </label>
    </div>
    <div class="placement-controls">
      <label>
        <span>Company profile</span>
        <select bind:value={placementProfile} disabled={!placementEnabled} data-material="tint" data-testid="intent-form-placement-profile">
          <option value="compact-control">Compact Control</option>
          <option value="thermal-first">Thermal First</option>
        </select>
      </label>
      <label>
        <span>Proposal policy</span>
        <select bind:value={placementPolicy} disabled={!placementEnabled} data-material="tint" data-testid="intent-form-placement-policy">
          <option value="deterministic">Deterministic · offline</option>
          <option value="gemini" disabled={placementPolicies.gemini === false}>Gemini · verifier gated</option>
          {#if experimentalPlacement}
            <option value="fast">Auto experimental</option>
            <option value="ollama" disabled={!placementPolicies.ollama}>Ollama · local</option>
            <option value="tinker" disabled={!placementPolicies.tinker}>Tinker · checkpoint</option>
            <option value="hybrid" disabled={!placementPolicies.hybrid}>Hybrid · local then Gemini</option>
          {/if}
        </select>
      </label>
    </div>
    <div class="experimental-row">
      <button
        type="button"
        class="experimental-toggle"
        class:active={experimentalPlacement}
        aria-pressed={experimentalPlacement}
        disabled={!placementEnabled}
        title="Reveal configured Ollama, Tinker, hybrid policy, and trace controls"
        onclick={toggleExperimental}
        data-testid="intent-form-experimental-placement"
      >Experimental features · {experimentalPlacement ? 'ON' : 'OFF'}</button>
      {#if experimentalPlacement}
        <label class="trace-consent">
          <input type="checkbox" bind:checked={recordTrace} />
          <span>Record verifier failure traces for later training</span>
        </label>
      {/if}
    </div>
  </section>

  <div class="controls">
    <label class="control">
      <span class="lbl">solver budget</span>
      <input
        class="mono budget"
        type="number"
        bind:value={timeLimit}
        disabled={noSolverBudget}
        min={MIN_TIME_LIMIT_S}
        max={MAX_TIME_LIMIT_S}
        step="1"
        data-testid="intent-form-budget"
        data-material="panel"
      />
      <span class="unit">{noSolverBudget ? 'unlimited' : 'seconds'}</span>
      <button
        type="button"
        class="no-budget"
        class:active={noSolverBudget}
        aria-pressed={noSolverBudget}
        data-testid="intent-form-no-budget"
        title="Let CP-SAT run without a solver time limit; deployment request limits may still apply"
        onclick={() => (noSolverBudget = !noSolverBudget)}
      >NO budget</button>
    </label>

    <label class="control checkbox">
      <input type="checkbox" bind:checked={review} data-testid="intent-form-review" />
      <span>Run the adversarial design review</span>
    </label>

    <label
      class="control checkbox"
      class:off={!hasDatasheets}
      title={hasDatasheets
        ? 'Findings are retrieved against the pages of the datasheets above'
        : 'Add a datasheet above — there is nothing to ground findings against'}
    >
      <input type="checkbox" bind:checked={ground} disabled={!hasDatasheets} data-testid="intent-form-ground" />
      <span>Ground findings against datasheet pages</span>
    </label>

    <label class="control checkbox">
      <input type="checkbox" bind:checked={enclosure} data-testid="intent-form-enclosure" />
      <span>Also generate a 3D-printable case</span>
    </label>

    {#if enclosure}
      <label
        class="control checkbox rigorous"
        title="Run the slower, exhaustive fit verification instead of the fast demo checks"
      >
        <input type="checkbox" bind:checked={enclosureRigorous} data-testid="intent-form-enclosure-rigorous" />
        <span>rigorous fit checks (slower)</span>
      </label>

      <label class="control case-style" title="Free-text styling for the case; left empty, the model chooses">
        <span class="lbl">case style</span>
        <input
          class="mono style"
          type="text"
          bind:value={enclosureStyle}
          maxlength={MAX_ENCLOSURE_STYLE_CHARS}
          placeholder="rounded corners, vented lid"
          aria-label="Case style"
          data-testid="intent-form-enclosure-style"
          data-material="panel"
        />
      </label>
    {/if}

    <label
      class="control checkbox"
      title="One more model call proposes a manufacturer and MPN per part, and each datasheet URL is fetched to check it is a PDF. MPNs stay proposals; no distributor is asked."
    >
      <input type="checkbox" bind:checked={sourcing} data-testid="intent-form-sourcing" />
      <span>Source the parts <span class="hint-inline">(one more model call + datasheet probes)</span></span>
    </label>

    <div class="spacer"></div>
    <!-- Dictation appends to the intent, so it edits the request the same way
         typing does and has to clear the constraint approval the same way. -->
    <MicButton
      context="intent"
      onappend={(text) => {
        intent = appendTranscript(intent, text)
        invalidateConstraintApproval()
      }}
    />
    <button type="submit" class="run" data-testid="intent-form-submit" disabled={!canSubmit}>
      Generate board
    </button>
  </div>
</form>

<style>
  .form { max-width: var(--measure-detail); }

  textarea {
    display: block;
    width: 100%;
    margin-top: 8px;
    padding: 12px 14px;
    background: var(--surface);
    border: 1px solid var(--rule-soft);
    font-size: var(--fs-body);
    line-height: 1.55;
    resize: none;
    overflow: hidden;
  }
  textarea:focus { border-color: var(--rule); outline: none; }

  .counter {
    margin-top: 6px;
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    color: var(--ink-faint);
  }
  .counter.over { color: var(--sev-blocker-fg); }

  .constraints { margin-top: 18px; padding: 13px 14px; border: 1px solid var(--rule-soft); background: var(--well); }
  .constraints > summary { display: flex; align-items: baseline; gap: 10px; }
  .summary-note, .constraint-actions, .detail-note { color: var(--ink-soft); font-size: var(--fs-ui); }
  .constraint-intro, .constraint-actions, .net-class-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
  .constraint-intro { margin-top: 13px; }
  .constraint-intro p { margin-top: 4px; color: var(--ink-soft); font-size: var(--fs-ui); }
  .constraint-switch, .constraint-check, .approval { display: flex; flex-direction: row; align-items: center; gap: 7px; color: var(--ink-mid); font-size: var(--fs-ui); }
  .constraint-actions { margin-top: 12px; }
  .constraint-actions button, .delete { padding: 5px 8px; border: 1px solid var(--rule); background: transparent; color: var(--ink-mid); font-size: 11px; }
  .board-layers { width: 180px; margin-top: 12px; }
  .net-class { margin-top: 13px; padding: 12px; border: 1px solid var(--rule-soft); background: var(--surface); }
  .constraint-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }
  .constraint-field { display: flex; flex-direction: column; gap: 5px; min-width: 0; color: var(--ink-mid); font-size: var(--fs-ui); }
  .constraint-field input, .constraint-field select, .mechanical-row input { width: 100%; min-width: 0; padding: 7px 8px; border: 1px solid var(--rule-soft); background: var(--surface); color: var(--ink); }
  .constraint-field small { color: var(--ink-faint); }
  .span-two { grid-column: span 2; }
  .constraint-detail { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--rule-soft); }
  .constraint-detail > summary { color: var(--ink); font-size: var(--fs-ui); font-weight: 600; }
  .constraint-check { align-self: end; min-height: 32px; }
  .constraint-add { margin-top: 9px; }
  .mechanical h4 { margin: 14px 0 6px; font-size: var(--fs-ui); }
  .mechanical-row { display: grid; gap: 7px; margin: 7px 0; }
  .keepout-row { grid-template-columns: 1.3fr repeat(4, 1fr) auto; }
  .fixed-row { grid-template-columns: 1.3fr repeat(3, 1fr) auto; }
  .pair-row { grid-template-columns: 1fr 1fr auto; max-width: 480px; }
  .approval { align-items: flex-start; margin-top: 14px; color: var(--ink); }
  .constraint-warning { margin: 8px 0 0; color: var(--sev-marginal-fg); font-size: var(--fs-ui); }

  .sheets { margin-top: 18px; border-top: 1px solid var(--rule-soft); padding-top: 14px; }
  summary { cursor: pointer; }
  .presets { margin-top: 12px; }
  .preset-heading { margin-bottom: 6px; }
  .preset {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 9px 10px;
    background: var(--surface);
    border: 1px solid var(--rule-soft);
  }
  .preset-copy { display: flex; flex-direction: column; min-width: 0; }
  .preset-part { color: var(--ink); }
  .preset-maker { font-size: 12px; color: var(--ink-soft); }
  .pdf { margin-left: auto; font-size: 12px; color: var(--ink-mid); white-space: nowrap; }
  .use {
    padding: 5px 10px;
    background: transparent;
    color: var(--ink-mid);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    font-size: 12px;
    white-space: nowrap;
  }
  .use:disabled { color: var(--ink-faint); border-color: var(--rule-soft); }
  .sheet-rows { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
  .sheet-row { display: flex; gap: 8px; }
  .sheet-row input {
    background: var(--surface);
    border: 1px solid var(--rule-soft);
    padding: 7px 10px;
    font-size: var(--fs-mono);
  }
  .part { width: 90px; }
  .url { flex-grow: 1; min-width: 0; }

  .add {
    align-self: flex-start;
    font-size: 12px;
    padding: 5px 11px;
    background: transparent;
    color: var(--ink-mid);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
  }

  .orchestrator {
    margin-top: 18px;
    padding: 14px;
    border: 1px solid var(--rule);
    background: var(--well);
  }
  .orchestrator-controls { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 12px; }
  .orchestrator-control { display: flex; flex-direction: column; gap: 6px; color: var(--ink-mid); font-size: var(--fs-ui); }
  .orchestrator-control select {
    width: 100%;
    padding: 8px 10px;
    border: 1px solid var(--rule-soft);
    background: var(--surface);
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: var(--fs-mono);
  }
  .orchestrator-control small { color: var(--ink-faint); line-height: 1.4; }
  .model-warning { margin-top: 10px; color: var(--sev-marginal-fg); font-size: var(--fs-ui); }

  .placement-settings { margin-top: 14px; padding: 13px 14px; border: 1px solid var(--rule-soft); background: var(--surface); }
  .placement-heading, .experimental-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
  .placement-heading p { margin-top: 4px; color: var(--ink-soft); font-size: var(--fs-ui); }
  .placement-on, .trace-consent { display: flex; align-items: center; gap: 7px; font-size: var(--fs-ui); color: var(--ink-mid); }
  .placement-controls { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
  .placement-controls label { display: flex; flex-direction: column; gap: 5px; color: var(--ink-mid); font-size: var(--fs-ui); }
  .placement-controls select { min-height: 38px; padding: 0 9px; border: 1px solid var(--rule); background: var(--well); color: var(--ink); }
  .experimental-row { margin-top: 11px; justify-content: flex-start; }
  .experimental-toggle { min-height: 34px; padding: 0 10px; border: 1px solid var(--rule); background: transparent; color: var(--ink-soft); font-size: 11px; }
  .experimental-toggle.active { border-color: var(--navy); color: var(--navy); }

  .controls {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-top: 22px;
    padding-top: 16px;
    border-top: 1px solid var(--rule-soft);
    flex-wrap: wrap;
  }
  .control { display: flex; align-items: center; gap: 8px; }
  .checkbox { font-size: var(--fs-ui); color: var(--ink-mid); cursor: pointer; }
  .checkbox.off { color: var(--ink-faint); cursor: default; }
  .rigorous { font-size: var(--fs-mono-sm); color: var(--ink-soft); }
  .hint-inline { color: var(--ink-soft); font-size: var(--fs-mono-sm); }
  .unit { font-size: var(--fs-ui); color: var(--ink-soft); }

  .budget {
    width: 62px;
    background: var(--surface);
    border: 1px solid var(--rule-soft);
    padding: 6px 8px;
    font-size: var(--fs-mono);
  }
  .budget:disabled { color: var(--ink-faint); }

  .style {
    width: 220px;
    background: var(--surface);
    border: 1px solid var(--rule-soft);
    padding: 6px 8px;
    font-size: var(--fs-mono);
  }
  .style:focus { border-color: var(--rule); outline: none; }

  .no-budget {
    padding: 6px 9px;
    background: transparent;
    border: 1px solid var(--rule);
    color: var(--ink-mid);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
  }
  .no-budget.active { background: var(--ink); color: var(--paper); }

  .spacer { flex-grow: 1; }

  .run {
    height: var(--mic-slot);
    font-size: var(--fs-body);
    font-weight: 500;
    padding: 0 20px;
    background: var(--accent);
    color: var(--accent-ink);
    border: none;
    border-radius: var(--radius);
  }
  .run:disabled { background: var(--accent-off); color: var(--accent-off-ink); }
  @media (max-width: 680px) {
    .orchestrator-controls, .placement-controls { grid-template-columns: 1fr; }
    .placement-heading, .experimental-row { align-items: flex-start; flex-direction: column; }
    .constraint-grid { grid-template-columns: 1fr; }
    .span-two { grid-column: span 1; }
    .constraint-intro, .constraint-actions { align-items: flex-start; flex-direction: column; }
    .mechanical-row { grid-template-columns: 1fr; }
  }
</style>
