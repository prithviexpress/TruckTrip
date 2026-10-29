/**
 * Creates the TripPlanning module (domain model only) in a Mendix app on the
 * Team Server, using the Mendix Model SDK.
 *
 *   export MENDIX_TOKEN=<personal access token with mx:modelrepository:repo:write>
 *   npm install
 *   npx tsx create_model.ts <AppId> [branch=main]
 *
 * Afterwards open the app in Studio Pro: the module, entities and associations
 * are there. The Java action and microflows are added in Studio Pro (see README).
 * Untested against a live Team Server: the SDK API names below follow the
 * documented Model SDK, check against your SDK version if a call fails.
 */
import { MendixPlatformClient } from "mendixplatformsdk";
import { domainmodels, projects, IModel } from "mendixmodelsdk";

const [appId, branch = "main"] = process.argv.slice(2);
if (!appId) { console.error("usage: tsx create_model.ts <AppId> [branch]"); process.exit(1); }

type Attr = [name: string, kind: "str" | "int" | "dec" | "bool" | "dt" | "long", size?: number];

const ENTITIES: Record<string, Attr[]> = {
  PlanSettings: [["HistoryWeeks", "int"], ["QStart", "dec"], ["QStop", "dec"], ["GraceMin", "int"],
    ["RoundMin", "int"], ["MinTrucks", "int"], ["MinRunPct", "int"], ["AllowancePct", "int"],
    ["FixedMin", "int"], ["QStopMinutes", "dec"], ["ManualMin", "int"], ["GapMin", "int"],
    ["EarlyCapH", "int"], ["LateCapH", "int"], ["FallbackRange", "int"]],
  GateEntry: [["TruckId", "str", 30], ["Vendor", "str", 20], ["Slot", "str", 5], ["SupplyDate", "dt"],
    ["GateIn", "dt"], ["GateOut", "dt"], ["Bay", "str", 20], ["Item", "str", 40],
    ["ReceivedQty", "dec"], ["PlannedQty", "dec"], ["ScheduleNo", "str", 30]],
  ItemStandard: [["Item", "str", 40], ["PackSize", "dec"], ["StdMinutes", "dec"], ["Basis", "str", 20],
    ["IsCritical", "bool"]],
  Bay: [["Code", "str", 20], ["LetterGroup", "str", 10], ["Number", "int"], ["IsHotLane", "bool"],
    ["Occupied", "bool"], ["FreeAt", "dt"]],
  BayFallback: [["Rank", "int"]],
  Trip: [["TruckId", "str", 30], ["Vendor", "str", 20], ["Slot", "str", 5], ["SupplyDate", "dt"],
    ["GateIn", "dt"], ["OffsetMin", "dec"], ["IsCritical", "bool"], ["Dropped", "bool"]],
  VendorSlotPlan: [["Vendor", "str", 20], ["Slot", "str", 5], ["Weekday", "str", 3],
    ["SafeStart", "dt"], ["HardStop", "dt"], ["SafeStartOffset", "int"], ["HardStopOffset", "int"],
    ["Basis", "str", 40], ["TrucksInHistory", "int"], ["RunsPct", "int"], ["PlanVersion", "int"],
    ["GeneratedOn", "dt"], ["IsActive", "bool"]],
  PlanStop: [["Sequence", "int"], ["StopMinutes", "int"], ["MinutesBasis", "str", 40],
    ["IsCritical", "bool"], ["SharePct", "int"]],
  Allocation: [["Start", "dt"], ["ExpectedEnd", "dt"], ["ActualEnd", "dt"], ["Swapped", "bool"],
    ["Reason", "str", 100]],
  ExceptionLog: [["Type", "str", 30], ["Reason", "str", 200], ["Who", "str", 50], ["When", "dt"]],
};

// child -> parent (many-to-one)
const ASSOC: [string, string, string][] = [
  ["BayFallback", "Bay", "BayFallback_Bay"],
  ["BayFallback", "Bay", "BayFallback_FallbackBay"],
  ["PlanStop", "VendorSlotPlan", "PlanStop_VendorSlotPlan"],
  ["PlanStop", "Bay", "PlanStop_Bay"],
  ["Trip", "VendorSlotPlan", "Trip_VendorSlotPlan"],
  ["Allocation", "Trip", "Allocation_Trip"],
  ["Allocation", "PlanStop", "Allocation_PlanStop"],
  ["Allocation", "Bay", "Allocation_Bay"],
  ["ExceptionLog", "Trip", "ExceptionLog_Trip"],
];

function attrType(model: IModel, kind: Attr[1], size?: number) {
  switch (kind) {
    case "str": { const t = domainmodels.StringAttributeType.create(model); t.length = size ?? 200; return t; }
    case "int": return domainmodels.IntegerAttributeType.create(model);
    case "long": return domainmodels.LongAttributeType.create(model);
    case "dec": return domainmodels.DecimalAttributeType.create(model);
    case "bool": return domainmodels.BooleanAttributeType.create(model);
    case "dt": return domainmodels.DateTimeAttributeType.create(model);
  }
}

async function main() {
  const client = new MendixPlatformClient();
  const app = client.getApp(appId);
  const wc = await app.createTemporaryWorkingCopy(branch);
  const model = await wc.openModel();

  const module = projects.Module.createIn(model.root);
  module.name = "TripPlanning";
  const dm = domainmodels.DomainModel.createIn(module);

  const ents: Record<string, domainmodels.Entity> = {};
  let i = 0;
  for (const [name, attrs] of Object.entries(ENTITIES)) {
    const e = domainmodels.Entity.createIn(dm);
    e.name = name;
    e.location = { x: 100 + (i % 4) * 300, y: 100 + Math.floor(i / 4) * 250 };
    i++;
    for (const [an, kind, size] of attrs) {
      const a = domainmodels.Attribute.createIn(e);
      a.name = an;
      a.type = attrType(model, kind, size);
    }
    ents[name] = e;
  }
  for (const [child, parent, name] of ASSOC) {
    const as = domainmodels.Association.createIn(dm);
    as.name = name;
    as.parent = ents[child];
    as.child = ents[parent];
    as.type = domainmodels.AssociationType.Reference;
    as.owner = domainmodels.AssociationOwner.Default;
  }

  await model.flushChanges();
  await wc.commitToRepository(branch, { commitMessage: "Add TripPlanning domain model" });
  console.log("TripPlanning module committed to", branch);
}
main().catch(e => { console.error(e); process.exit(1); });
