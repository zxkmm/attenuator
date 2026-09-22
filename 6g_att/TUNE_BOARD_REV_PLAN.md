# Next revision: button-tunable TUNE board

Context this plan is built on (from `attenuator_calc.py`'s own docstring and
constants — not assumptions):

```
PA (5-10 W CW) -> POWER board (20 dB fixed) -> TUNE board (dual Pi) -> spectrum analyser
```

- The **POWER board** (this board, `6g_att`) absorbs the PA's full power and
  drops it a fixed 20 dB. It stays exactly as-is in this revision.
- The **TUNE board** only ever sees the POWER board's *output* — at 10 W in,
  that's 100 mW. `TUNE_PART_LIMIT_W = 0.15` in the calculator already assumes
  this (0603 parts at <=60% of a 1/4 W rating). This is the board that becomes
  button-tunable.
- Today the TUNE board hits an arbitrary dB value by hand-picking a "main" (a)
  + "trim" (b) resistor pair per shunt node from an E-series (see
  `design()`/`tune_elements()`) — precise, but fixed at manufacture. The job
  of this revision is to make that selection happen electronically, at the
  press of a button, instead of at assembly.

Because the power here is genuinely low (~100 mW, not the 10 W the POWER
board deals with), this is a much easier switching problem than it might
first look like — no relays, no PIN-diode bias networks needed.

## Recommended architecture: digital step attenuator IC, not a discrete switched ladder

Use a purpose-built **digital step attenuator (DSA) IC** (e.g. pSemi/PE45xx
or PE43xx family, or Analog Devices HMC/ADRF DSA parts — pick one specified
to DC-6 GHz, since many cheaper DSAs only spec to 3-4 GHz) in place of the
hand-tuned dual-pi resistor network:

- One QFN part replaces the entire R1a/R1b/Rs1/R2a/R2b/Rs2/R3a/R3b network,
  the E-series picking logic, and the trim-resistor calibration trick.
- Typical parts give 0.5-1 dB steps across 30-31.5 dB range, controlled by
  SPI or parallel GPIO — a direct match for `table`'s existing 1-30 dB
  reference range.
- Power handling on these parts is normally rated well above 100 mW (often
  1-2 W CW), so no derating concern at this board's actual power level.
- Far fewer parts, far less new PCB layout risk than routing N discrete RF
  switches. This is how commercial step attenuators at this power/frequency
  class are actually built.

**Trade-off to accept knowingly:** you're now dependent on a single specialty
RF IC (cost, lead time, single-source risk) instead of commodity resistors +
relays. If that's unacceptable, see the discrete alternative below.

### Why not relays or a hand-rolled switched resistor ladder

- RF relays make sense on the POWER board's power budget (10 W), not here —
  at 100 mW they're needlessly bulky, slow, and expensive for this job.
- A discrete binary-weighted switched pi-ladder (1/2/4/8/16 dB bypassable
  sections, PIN diode or small RF-switch-IC per section) is buildable and
  would reuse this session's resistor-ladder placement tooling, but it's
  meaningfully more design/layout/verification effort than one DSA IC for no
  real benefit at this power level. Worth it only if you specifically want
  to avoid a single-vendor RF IC, want firmware-only control of every
  resistor value (no fixed IC step table), or want this as a learning
  exercise.

## Control (onboard MCU — confirmed)

- Small MCU (e.g. STM32 or similar already-familiar part) on the TUNE board:
  - Reads buttons (up/down, or up/down + a "coarse/fine" modifier), debounced
    in firmware.
  - Drives the DSA IC's control interface (SPI is simplest — 3-4 GPIO).
  - Displays current dB setting (7-segment or small OLED/LED bar) so the
    setting is visible without a host PC.
  - Persists last setting in flash/EEPROM; defines a safe power-up default
    (recommend **max attenuation at power-up**, so nothing downstream sees an
    unexpectedly low-loss path on boot).
  - Optional: expose the same control over UART/USB so it can also be driven
    from a PC (useful for the `correct` amplitude-calibration workflow the
    calculator already supports).

## RF layout considerations

- DSA ICs at DC-6 GHz are sensitive to their reference layout (matched
  50 ohm lines in/out, via stitching, ground pour under the exposed pad,
  decoupling per datasheet) — follow the manufacturer's eval-board layout
  closely rather than improvising; this is the highest RF-risk part of the
  revision.
- Keep the MCU/button/display digital section physically separated from the
  RF signal path, with a clear ground boundary, same discipline as the
  existing POWER board's RF-vs-silkscreen layout.
- The IC replaces both series legs and all three shunt nodes, so the
  existing `place_all_pi.py` / `fix_neck_gaps.py` resistor-ladder tooling
  from this session isn't needed for the DSA's own footprint — but it's
  still exactly the right tool if the eval later shows you want to add fixed
  pad(s) before/after the IC (e.g. an input pad for extra isolation).

## Suggested build-out order (de-risk the RF part first)

1. Pick a specific DSA IC rated to >=6 GHz; get its eval board or a breakout,
   confirm insertion loss / return loss / step accuracy at 6 GHz match the
   datasheet before committing to a full board layout.
2. Prototype the MCU + button + display control loop separately (breadboard
   or a small carrier board) against the IC's eval board — get firmware
   (debounce, step logic, persistence, power-up default) working first.
3. Only then lay out the full TUNE board revision combining both, reusing
   the POWER board's RF layout conventions.
4. Update `attenuator_calc.py`'s `chain`/`table` commands (or add a new one)
   to reflect the IC's actual step table instead of the E-series trim-pair
   math, so the existing calibration/correction workflow (`correct`) keeps
   working against the new board.

## Open items for you to confirm before layout starts

- Exact DSA IC choice (drives package, control interface pin count, price).
- Button UI: up/down only, or direct-entry/presets too; single button pair
  vs a small keypad.
- Display: 7-segment vs small graphic display vs LEDs only.
- Whether MCU firmware needs a host-PC interface (UART/USB) for the
  calibration workflow, or is fully standalone.
