# Bio-Agri Process Simulator — V0.24

Application version: **0.24.7**
Engineering model version: **0.24.4**
Schema version: **1.0.0**

Bio-Agri is a local engineering-screening application for lignocellulosic ethanol process development. It combines a visual flowsheet, transparent equipment assumptions, validated mass-balance calculations, scenario comparison, sensitivity analysis, and plant-level utility summaries.

## Intended use

Use the simulator for screening, option comparison, assumption management, and early engineering conversations. It is not a replacement for detailed design, a validated commercial property package, equipment-vendor design, HAZOP, relief design, or a stage-by-stage process simulator.

## Starting the application

Run `RUN_SIMULATOR_WINDOWS.bat`. The launcher stops earlier simulator processes, checks dependencies, copies the frontend source into a reliable local runtime, starts both services, verifies readiness, and opens the browser.

## Interface modes

- **Simple** presents the process route, principal inputs, results, and plain-language warnings.
- **Engineering** exposes advanced parameters, calculation bases, confidence statements, and detailed block results.

## Decision-support tools

- Issues and assumptions register
- Prepared engineering scenarios
- Current-route versus reference comparison
- One-at-a-time sensitivity analysis
- Pressure-aware thermodynamic summaries
- Native batch/vessel scheduling with fill, heat/pretreat, reaction, empty and CIP states
- Native hydrolysis and fermentation time profiles calibrated to selected endpoint performance
- Dynamic heat, cooling and electrical-demand summaries with coincident peaks
- Batch-capacity, utilisation and bottleneck checks
- Exportable flowsheet JSON
- Dedicated stream, heat-duty, and electrical-demand tables
- PDF-ready engineering report containing all engineering tables
- Animated 168-hour digital-twin replay with vessel graphics, calculated liquid levels, live operating states, transfer activity and utility readings

## Validation

Run `python -m unittest discover -v`. This executes the discoverable application regression suite. The legacy version-specific scripts remain available for detailed numerical output.

## Data safety

The interface warns before discarding unsaved changes. Use **View plant results** to open the Summary, Stream table, Heat data, and Electrical data tabs. Select **Print / save tables as PDF** to open the complete engineering report, then use its print control to save a PDF.

## V0.15 engineering model notes

V0.15 advances the process-physics layer without changing the validated core mass balance.

## Pressure-aware ethanol/water thermodynamics
The simulator now includes:
- extended Antoine vapor-pressure correlations for ethanol and water
- a non-ideal NRTL activity-coefficient model
- pressure-aware bubble points and equilibrium vapor compositions
- local relative volatility
- Fenske minimum-stage screening
- a pressure-aware rectifier screening calculation

The NRTL interaction parameters are an internal fit to the current 1-atm reference VLE dataset. This remains a screening property model and should be validated against published or licensed experimental VLE/enthalpy data. No external process simulator is required by the application architecture.

## Column pressure basis
- Beer column D-501: 2 atm, directly supported by NREL 2011.
- Rectifier D-502: 1.77 atm is retained as a configurable screening basis from legacy NREL practice because the 2011 design text does not explicitly state a D-502 overhead pressure.

The beer column remains a vapor-side-draw system, so it is not incorrectly treated as a simple conventional binary distillation column.

## Dynamic heat recovery
Pretreatment heat recovery is now aligned against the batch schedule rather than simply subtracted as an average duty.

For each timestep the model resolves:
- vessels heating
- vessels discharging hot slurry
- gross batch heat demand
- available recovered heat
- recovered heat actually used
- net external pretreatment heat
- continuous distillation heat
- total external thermal load
- steam demand

This produces separate average and coincident peak steam loads.

## Distillation duty
The pressure-aware rectifier model calculates a thermodynamic screening duty, but it does not replace the existing NREL-normalised total distillation duty because:
- the beer column is a side-draw configuration
- sensible heat, feed quality and internal heat integration are not fully solved
- a RADFRAC-equivalent stage-by-stage enthalpy model has not yet been implemented

The simulator therefore reports the new result alongside the current reference duty.

## Validation
V0.15 passes:
- Excel parity regression
- V0.13 scenario/calibration tests
- V0.14 VLE/energy tests
- new pressure-aware NRTL and dynamic-energy tests

## V0.18 native dynamic plant engine
V0.18 removes the external-simulator architectural dependency and adds a native time-domain operating layer. Every standard run now calculates pretreatment, hydrolysis and fermentation vessel cycles using the active flowsheet parameters. The engine reports installed versus required vessel count, cycle time, batch mass, capacity, utilisation, bottleneck status and a 168-hour state timeline.

Pretreatment heat demand is resolved against simultaneous hot discharge so recovered heat is only credited when a usable hot source and cold demand coincide. Hydrolysis and fermentation include first-order empirical time profiles calibrated exactly to the selected endpoint conversion/yield. These are transparent screening kinetics and are intentionally not presented as fundamental reaction kinetics until the miscanthus trial programme supplies sufficient data for calibration.

After running the model, open **View plant results → Operations** to see plant feasibility, the current bottleneck, installed-versus-required vessel capacity, utilisation bars, phase timelines, and dynamic heat, electricity, cooling and heat-recovery profiles.

V0.18.1 makes pretreatment, hydrolysis and fermentation fill/empty durations pump-driven. Each duration is calculated as vessel working volume divided by its selected inlet or outlet transfer-pump capacity. Optional motor kW values are applied only while the relevant transfer phase is active. The reference pump rates preserve the previous validated cycle times; motor demand remains zero until pump head, efficiency and vendor selection establish a defensible basis. Newly added batch blocks start with editable 15 m³/h inlet and outlet pump capacities.

## V0.19 animated digital twin
Open **View plant results → Digital twin** after a run. The twin replays every 15-minute row in the native 168-hour calculation and provides play, pause, step, speed and time-scrub controls. Pretreatment, hydrolysis and fermentation vessels show the current calculated operating phase and a phase-derived liquid-level animation. Transfer pipes respond to active fill/empty states, while external heat, recovered heat, electricity and cooling readings come directly from the selected timeline row. The recovery train is explicitly labelled as continuous screening.

This first twin is a truthful visual replay of the current independently staggered schedule. It does not yet claim inventory conservation between vessels or solve shared-pump contention, buffer levels, starvation or downstream blocking. Those require the planned discrete-event material-transfer engine.

The V0.18 scheduler staggers vessels independently within each process section. Individual vessel transfer time and optional transfer motor load are modelled, but the scheduler does not yet enforce upstream material availability, downstream capacity, intermediate buffers, shared-pump contention, starvation, blocking, batch hand-offs, or shared utility-resource constraints. Those remain requirements for the next discrete-event scheduler stage.

## Remaining model-development roadmap
A future engineering-model release should:
1. calibrate pretreatment and enzymatic-hydrolysis kinetics against the miscanthus trial programme and literature,
2. calibrate fermentation dynamics against measured glucose, ethanol and CO2 time-series data,
3. implement native stage-by-stage rectifier MESH screening,
4. build a dedicated native beer-column side-draw model,
5. calculate reboiler/condenser duties from native enthalpy correlations and feed them directly into the dynamic steam/cooling system,
6. add shared-pump contention and buffer-vessel constraints so upstream/downstream batch interactions are event-resolved rather than independently staggered.


## V0.20.1 spreadsheet alignment
V0.20.1 aligns the application reference route to the master Google Sheet P01–P12 process basis. P03 Pretreatment Heat Recovery & Cooling is now an explicit post-pretreatment stage; P07 represents beer-column-bottoms economising; P08/P09 expose the workbook tray, efficiency, feed/side-draw and reflux inputs; and the P10 molecular-sieve recycle is identified as a specified tear stream back to P09 tray 14 pending iterative recycle convergence. The Google Sheet is reference-only and is not modified by the application release.


## V0.20.2 native files and integrated recovery
V0.20.2 replaces packaged-backend persistence as the normal desktop workflow with native **Open**, **Save**, and **Save As** file dialogs. BioAgri flowsheets are saved directly to user-selected disk locations as JSON, preserving block positions, parameters and connections, and subsequent Save operations write back to the open file. Legacy backend-saved flows remain accessible for migration.

Process-model changes include explicit P07 cold-side economiser energy closure (required duty, recovered duty and external trim heat) and an iteratively converged P10 molecular-sieve regeneration recycle to P09 using the workbook 72 wt% ethanol tear-stream basis. Internal recycle is excluded from external plant material closure; the workbook P11 product basis remains 643.8334866 L/h.


## V0.21 native distillation screening
V0.21 adds a native binary ethanol/water shortcut-distillation model for P08 and P09. The application preserves the master workbook mass-balance targets while independently calculating Fenske minimum stages, Underwood minimum reflux, Gilliland required theoretical stages, installed effective stages, stage margin, representative relative volatility and constant-molar-overflow screening reboiler/condenser duties. P08 is a stripper with a vapour side draw, so its FUG result is presented as a diagnostic rather than a definitive column design verdict; P09 is a more conventional rectification application.

The representative volatility calculation uses NIST Chemistry WebBook Antoine vapour-pressure coefficients for ethanol and water. The shortcut remains an engineering screening model: it is binary, does not implement an activity-coefficient model for the ethanol/water azeotrope, and is not a substitute for vendor or rigorous rate-based column design.


## V0.22 Digital Twin and Scheduler
V0.22 promotes the Digital Twin and production Scheduler to full-page primary workspaces rather than result-sheet tabs. The Digital Twin retains the P01-P12 plant replay while the Scheduler exposes batch capacity, phase timing, transfer pumps, operability events and utility demand in a dedicated workspace.

The connected discrete-event engine now interpolates vessel inventory through transfer duration, so FILLING and EMPTYING states visibly change vessel level instead of jumping at transfer completion. No intermediate buffer vessels are introduced; P02 to P04 to P05 transfers remain direct vessel-to-vessel.

Repository validation no longer runs on every development-branch push; it runs on pull requests to main or manually, reducing CI notification noise while retaining merge gates.


## V0.23 Digital Twin 2.0
V0.23 makes the operational views first-class engineering workspaces. The Digital Twin now exposes every installed P02/P04/P05 batch vessel individually with continuous fill level, live state, batch lineage, active direct-transfer endpoints and pump ownership. The Scheduler adds a full per-vessel Gantt based on actual event-engine state occupancy rather than representative cycle bars alone.

A new Plant Dashboard provides production, scheduled throughput, utilities, specific energy, material-balance health, bottleneck utilisation, scheduler health and open engineering assumptions in one landing view. The connected event engine remains the source of truth: direct P02→P04→P05 transfers are retained and no intermediate buffer vessels are invented.

The Windows release workflow also pre-caches NSIS with retry logic to reduce transient packaging failures. Development validation remains PR/manual only to reduce notification noise.
