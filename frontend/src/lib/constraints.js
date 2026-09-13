const SIGNAL_KINDS = new Set(['signal', 'i2c', 'usb', 'ethernet', 'spi', 'clock', 'power', 'analog', 'rf'])
const SOFT_FIELDS = [
  'fewer_vias',
  'shorter_traces',
  'compact_grouping',
  'thermal_separation',
  'connector_accessibility',
]

const EXAMPLES = {
  signal: { nets: 'STATUS_LED', allowed_layers: 'F.Cu, B.Cu', max_layer_transitions: '2', max_vias_per_net: '2' },
  i2c: {
    nets: 'SDA, SCL', allowed_layers: 'F.Cu, B.Cu', max_layer_transitions: '2',
    max_vias_per_net: '2', signal_voltage_v: '3.3', max_frequency_hz: '400000',
    pullup_rail: '3V3', pullup_min_ohms: '1000', pullup_max_ohms: '10000',
    bus_capacitance_pf: '50', max_rise_time_ns: '300',
  },
  usb: {
    nets: 'USB_D+, USB_D-', allowed_layers: 'F.Cu', max_layer_transitions: '0',
    max_vias_per_net: '0', impedance_ohms: '90', impedance_tolerance_percent: '10',
    pair_spacing_mm: '0.2', reference_plane: 'GND', max_skew_mm: '0.5',
  },
  ethernet: {
    nets: 'TX+, TX-, RX+, RX-', allowed_layers: 'F.Cu', max_layer_transitions: '0',
    max_vias_per_net: '0', impedance_ohms: '100', impedance_tolerance_percent: '10',
    pair_spacing_mm: '0.2', reference_plane: 'GND', max_skew_mm: '0.5',
  },
  spi: {
    nets: 'SCLK, MOSI, MISO, CS', allowed_layers: 'F.Cu, B.Cu',
    max_layer_transitions: '2', max_vias_per_net: '2', max_frequency_hz: '10000000',
    max_length_mm: '50', max_skew_mm: '5', max_stub_length_mm: '5', reference_plane: 'GND',
  },
  clock: {
    nets: 'CLK', allowed_layers: 'F.Cu', max_layer_transitions: '0', max_vias_per_net: '0',
    max_frequency_hz: '25000000', max_length_mm: '50', max_skew_mm: '2',
    max_stub_length_mm: '2', reference_plane: 'GND',
  },
  power: {
    nets: 'VIN, VOUT, GND', allowed_layers: 'F.Cu, B.Cu', max_layer_transitions: '2',
    max_vias_per_net: '4', expected_current_a: '1', min_trace_width_mm: '0.5',
    copper_weight_oz: '1', max_voltage_drop_v: '0.1', min_thermal_separation_mm: '5',
  },
  analog: {
    nets: 'ADC_IN, AGND', allowed_layers: 'F.Cu', max_layer_transitions: '0',
    max_vias_per_net: '0', min_separation_mm: '0.5', reference_plane: 'AGND',
  },
  rf: {
    nets: 'RF_IN', allowed_layers: 'F.Cu', max_layer_transitions: '0', max_vias_per_net: '0',
    impedance_ohms: '50', impedance_tolerance_percent: '10', pair_spacing_mm: '0.5',
    reference_plane: 'GND', max_skew_mm: '1', max_length_mm: '30', min_separation_mm: '1',
  },
}

const CONCERNS = {
  signal: ['Confirm exact net names and measurable routing limits'],
  i2c: ['Pull-ups', 'rise time', 'bus capacitance'],
  usb: ['Differential impedance', 'pair skew', 'reference plane continuity'],
  ethernet: ['Differential impedance', 'pair skew', 'magnetics placement'],
  spi: ['Clock return path', 'length', 'skew', 'stubs'],
  clock: ['Return path', 'length', 'stubs'],
  power: ['Expected current', 'trace width', 'voltage drop', 'thermal path'],
  analog: ['Isolation', 'guard region', 'return path'],
  rf: ['Impedance', 'isolation', 'return path', 'keepouts'],
}

const NUMBER_FIELDS = [
  'signal_voltage_v', 'max_frequency_hz', 'pullup_min_ohms', 'pullup_max_ohms',
  'bus_capacitance_pf', 'max_rise_time_ns', 'impedance_ohms',
  'impedance_tolerance_percent', 'pair_spacing_mm', 'min_trace_width_mm',
  'max_length_mm', 'max_skew_mm', 'max_stub_length_mm', 'expected_current_a',
  'copper_weight_oz', 'max_voltage_drop_v', 'min_separation_mm',
  'min_thermal_separation_mm',
]
const MANIFEST_FIELDS = new Set(['version', 'approved', 'board_layers', 'net_classes', 'mechanical', 'soft_preferences'])
const NET_CLASS_FIELDS = new Set(Object.keys(newConstraintClass()))
const MECHANICAL_FIELDS = new Set(['max_board_width_mm', 'max_board_height_mm', 'max_component_height_mm', 'mounting_hole_refs', 'keepouts', 'fixed_placements'])
const KEEPOUT_FIELDS = new Set(['name', 'x_mm', 'y_mm', 'width_mm', 'height_mm'])
const FIXED_FIELDS = new Set(['ref', 'x_mm', 'y_mm', 'tolerance_mm'])

function objectOf(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
}

function text(value) {
  return typeof value === 'string' ? value.trim() : ''
}

function strings(value) {
  if (typeof value === 'string') value = value.split(',')
  if (!Array.isArray(value)) return []
  return value.map((item) => text(item)).filter(Boolean)
}

function finiteOrNull(value) {
  if (value === '' || value === null || value === undefined || typeof value === 'boolean') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function integerOrNull(value) {
  const number = finiteOrNull(value)
  return Number.isInteger(number) ? number : null
}

function unique(values) {
  return new Set(values).size === values.length
}

function hasOnlyKeys(value, allowed) {
  return Object.keys(objectOf(value)).every((key) => allowed.has(key))
}

function manifestHasUnknowns(raw) {
  const manifest = objectOf(raw)
  if (!hasOnlyKeys(manifest, MANIFEST_FIELDS)) return true
  const classes = Array.isArray(manifest.net_classes) ? manifest.net_classes : []
  if (classes.some((item) => !hasOnlyKeys(item, NET_CLASS_FIELDS))) return true
  if (classes.some((item) => item?.kind && !SIGNAL_KINDS.has(text(item.kind).toLowerCase()))) return true
  const mechanical = objectOf(manifest.mechanical)
  if (!hasOnlyKeys(mechanical, MECHANICAL_FIELDS)) return true
  if ((Array.isArray(mechanical.keepouts) ? mechanical.keepouts : []).some((item) => !hasOnlyKeys(item, KEEPOUT_FIELDS))) return true
  if ((Array.isArray(mechanical.fixed_placements) ? mechanical.fixed_placements : []).some((item) => !hasOnlyKeys(item, FIXED_FIELDS))) return true
  return !hasOnlyKeys(manifest.soft_preferences, new Set(SOFT_FIELDS))
}

function optionalNumberReady(value, { low = 0, positive = false } = {}) {
  if (value === null || value === '' || value === undefined) return true
  const number = finiteOrNull(value)
  return number !== null && (positive ? number > low : number >= low)
}

export function constraintKinds(intent) {
  const value = String(intent || '').toLowerCase()
  const kinds = []
  if (/\bi2c\b|\bsda\b|\bscl\b/.test(value)) kinds.push('i2c')
  if (/\busb\b|\bd\+\b|\bd-\b/.test(value)) kinds.push('usb')
  if (/\beth(?:ernet)?\b|\brgmii\b|\brmii\b/.test(value)) kinds.push('ethernet')
  if (/\bspi\b|\bsclk\b|\bmosi\b|\bmiso\b/.test(value)) kinds.push('spi')
  if (/\bclock\b|\bclk\b|\boscillator\b|\bcrystal\b/.test(value)) kinds.push('clock')
  if (/\bpower\b|\bmotor\b|\bregulator\b|\bcurrent\b|\bvin\b|\bvout\b/.test(value)) kinds.push('power')
  if (/\banalog\b|\badc\b|\bdac\b|\bsensor input\b/.test(value)) kinds.push('analog')
  if (/\brf\b|\bantenna\b|\bwireless\b|\b2\.4\s*ghz\b/.test(value)) kinds.push('rf')
  return kinds.length ? [...new Set(kinds)] : ['signal']
}

export function newConstraintClass(kind = 'signal') {
  const safeKind = SIGNAL_KINDS.has(String(kind).toLowerCase()) ? String(kind).toLowerCase() : 'signal'
  return {
    name: safeKind === 'signal' ? 'Critical signals' : `${safeKind.toUpperCase()} constraints`,
    kind: safeKind,
    nets: [],
    allowed_layers: [],
    max_layer_transitions: null,
    max_vias_per_net: null,
    signal_voltage_v: null,
    max_frequency_hz: null,
    pullups_required: false,
    pullup_rail: null,
    pullup_min_ohms: null,
    pullup_max_ohms: null,
    bus_capacitance_pf: null,
    max_rise_time_ns: null,
    controlled_impedance: false,
    impedance_ohms: null,
    impedance_tolerance_percent: null,
    pair_spacing_mm: null,
    reference_plane: null,
    min_trace_width_mm: null,
    max_length_mm: null,
    max_skew_mm: null,
    max_stub_length_mm: null,
    expected_current_a: null,
    copper_weight_oz: null,
    max_voltage_drop_v: null,
    min_separation_mm: null,
    min_thermal_separation_mm: null,
    thermal_pairs: [],
    guard_required: false,
    concerns: [...CONCERNS[safeKind]],
  }
}

export function constraintFieldExample(kind, field) {
  return EXAMPLES[SIGNAL_KINDS.has(kind) ? kind : 'signal']?.[field] || ''
}

export function suggestConstraintManifest(intent) {
  return {
    version: 2,
    approved: false,
    // The UI labels this as an editable example. Nothing is sent until the
    // engineer explicitly enables and approves the contract.
    board_layers: 2,
    net_classes: constraintKinds(intent).map(newConstraintClass),
    mechanical: {
      max_board_width_mm: null,
      max_board_height_mm: null,
      max_component_height_mm: null,
      mounting_hole_refs: [],
      keepouts: [],
      fixed_placements: [],
    },
    soft_preferences: Object.fromEntries(SOFT_FIELDS.map((name) => [name, 0])),
  }
}

function normalizeNetClass(raw) {
  const item = objectOf(raw)
  const requestedKind = text(item.kind).toLowerCase()
  const kind = SIGNAL_KINDS.has(requestedKind) ? requestedKind : 'signal'
  const normalized = {
    ...newConstraintClass(kind),
    name: text(item.name),
    kind,
    nets: strings(item.nets),
    allowed_layers: strings(item.allowed_layers),
    max_layer_transitions: integerOrNull(item.max_layer_transitions),
    max_vias_per_net: integerOrNull(item.max_vias_per_net),
    signal_voltage_v: finiteOrNull(item.signal_voltage_v ?? item.pullup_voltage_v),
    pullups_required: item.pullups_required === true,
    pullup_rail: text(item.pullup_rail) || null,
    controlled_impedance: item.controlled_impedance === true,
    reference_plane: text(item.reference_plane) || null,
    guard_required: item.guard_required === true,
    concerns: strings(item.concerns),
    thermal_pairs: (Array.isArray(item.thermal_pairs) ? item.thermal_pairs : [])
      .slice(0, 128)
      .map((pair) => (Array.isArray(pair) ? [text(pair[0]), text(pair[1])] : ['', ''])),
  }
  for (const name of NUMBER_FIELDS) {
    if (name !== 'signal_voltage_v') normalized[name] = finiteOrNull(item[name])
  }
  return normalized
}

function normalizeMechanical(raw) {
  const mechanical = objectOf(raw)
  const keepouts = Array.isArray(mechanical.keepouts) ? mechanical.keepouts : []
  const fixed = Array.isArray(mechanical.fixed_placements) ? mechanical.fixed_placements : []
  return {
    max_board_width_mm: finiteOrNull(mechanical.max_board_width_mm),
    max_board_height_mm: finiteOrNull(mechanical.max_board_height_mm),
    max_component_height_mm: finiteOrNull(mechanical.max_component_height_mm),
    mounting_hole_refs: strings(mechanical.mounting_hole_refs),
    keepouts: keepouts.slice(0, 128).map((rawItem) => {
      const item = objectOf(rawItem)
      return {
        name: text(item.name),
        x_mm: finiteOrNull(item.x_mm),
        y_mm: finiteOrNull(item.y_mm),
        width_mm: finiteOrNull(item.width_mm),
        height_mm: finiteOrNull(item.height_mm),
      }
    }),
    fixed_placements: fixed.slice(0, 128).map((rawItem) => {
      const item = objectOf(rawItem)
      return {
        ref: text(item.ref),
        x_mm: finiteOrNull(item.x_mm),
        y_mm: finiteOrNull(item.y_mm),
        tolerance_mm: finiteOrNull(item.tolerance_mm),
      }
    }),
  }
}

/** Normalize saved/imported manifests before a component dereferences them.
 * Version 1 is migrated to version 2 but loses approval because migration is a
 * manifest edit that the engineer must review. */
export function normalizeConstraintManifest(raw = {}) {
  const manifest = objectOf(raw)
  const soft = objectOf(manifest.soft_preferences)
  const version = Number(manifest.version)
  return {
    version: 2,
    approved: version === 2 && manifest.approved === true && !manifestHasUnknowns(manifest),
    board_layers: integerOrNull(manifest.board_layers),
    net_classes: (Array.isArray(manifest.net_classes) ? manifest.net_classes : [])
      .slice(0, 24)
      .map(normalizeNetClass),
    mechanical: normalizeMechanical(manifest.mechanical),
    soft_preferences: Object.fromEntries(
      SOFT_FIELDS.map((name) => [name, Math.max(0, finiteOrNull(soft[name]) ?? 0)]),
    ),
  }
}

function hasRequiredNumbers(item, names) {
  return names.every((name) => finiteOrNull(item[name]) !== null)
}

function netClassReady(raw) {
  const item = normalizeNetClass(raw)
  if (!item.name || !item.nets.length || !unique(item.nets)) return false
  if (!item.allowed_layers.length || !unique(item.allowed_layers)) return false
  if (!item.allowed_layers.every((layer) => ['F.Cu', 'B.Cu'].includes(layer))) return false
  if (!Number.isInteger(item.max_layer_transitions) || item.max_layer_transitions < 0) return false
  if (!Number.isInteger(item.max_vias_per_net) || item.max_vias_per_net < 0) return false
  if (item.pullups_required) {
    if (!item.pullup_rail || !hasRequiredNumbers(item, ['pullup_min_ohms', 'pullup_max_ohms'])) return false
    if (item.pullup_min_ohms <= 0 || item.pullup_max_ohms <= 0 || item.pullup_min_ohms > item.pullup_max_ohms) return false
  }
  if (item.controlled_impedance) {
    if (!item.reference_plane) return false
    if (!hasRequiredNumbers(item, ['impedance_ohms', 'impedance_tolerance_percent', 'pair_spacing_mm', 'max_skew_mm'])) return false
  }
  const required = {
    i2c: ['signal_voltage_v', 'max_frequency_hz', 'bus_capacitance_pf', 'max_rise_time_ns'],
    spi: ['max_frequency_hz', 'max_length_mm', 'max_skew_mm'],
    clock: ['max_frequency_hz', 'max_length_mm', 'max_skew_mm'],
    power: ['expected_current_a', 'min_trace_width_mm', 'copper_weight_oz', 'max_voltage_drop_v', 'min_thermal_separation_mm'],
    analog: ['min_separation_mm'],
    rf: ['min_separation_mm', 'max_length_mm'],
  }
  if (!hasRequiredNumbers(item, required[item.kind] || [])) return false
  if (['spi', 'clock', 'analog', 'rf'].includes(item.kind) && !item.reference_plane) return false
  if (!item.thermal_pairs.every((pair) => pair.length === 2 && pair[0] && pair[1] && pair[0] !== pair[1])) return false
  if (!unique(item.thermal_pairs.map((pair) => [...pair].sort().join('\u0000')))) return false
  return NUMBER_FIELDS.every((name) => optionalNumberReady(item[name]))
}

function mechanicalReady(mechanical) {
  const optionalPositive = ['max_board_width_mm', 'max_board_height_mm', 'max_component_height_mm']
  if (!optionalPositive.every((name) => optionalNumberReady(mechanical[name], { positive: true }))) return false
  if (!unique(mechanical.mounting_hole_refs)) return false
  if (!unique(mechanical.keepouts.map((item) => item.name))) return false
  if (!unique(mechanical.fixed_placements.map((item) => item.ref))) return false
  if (!mechanical.keepouts.every((item) => item.name
    && optionalNumberReady(item.x_mm)
    && optionalNumberReady(item.y_mm)
    && optionalNumberReady(item.width_mm, { positive: true })
    && optionalNumberReady(item.height_mm, { positive: true })
    && [item.x_mm, item.y_mm, item.width_mm, item.height_mm].every((value) => finiteOrNull(value) !== null))) return false
  return mechanical.fixed_placements.every((item) => item.ref
    && [item.x_mm, item.y_mm].every((value) => finiteOrNull(value) !== null && value >= 0)
    && finiteOrNull(item.tolerance_mm) !== null && item.tolerance_mm > 0)
}

export function constraintManifestReady(raw = {}) {
  const manifest = normalizeConstraintManifest(raw)
  if (manifest.board_layers !== 2) return false
  if (!manifest.net_classes.length || !unique(manifest.net_classes.map((item) => item.name))) return false
  if (!manifest.net_classes.every(netClassReady)) return false
  const ownedNets = manifest.net_classes.flatMap((item) => item.nets)
  if (!unique(ownedNets)) return false
  return mechanicalReady(manifest.mechanical)
}
