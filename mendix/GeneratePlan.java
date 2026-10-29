// Java action TripPlanning.GeneratePlan
// Parameters (set in Studio Pro): Settings (TripPlanning.PlanSettings), Weekday (String, "" = all days)
// Return type: Integer/Long (number of VendorSlotPlan rows written)
//
// Steps mirror trip_demand_analysis.py: trips -> offsets -> safe start / hard stop ->
// bay list -> stop minutes. Reads GateEntry + ItemStandard, writes VendorSlotPlan + PlanStop.
package tripplanning.actions;

import java.time.*;
import java.util.*;
import java.util.stream.*;
import com.mendix.core.Core;
import com.mendix.systemwideinterfaces.core.IContext;
import com.mendix.systemwideinterfaces.core.IMendixObject;
import com.mendix.webui.CustomJavaAction;
import tripplanning.proxies.*;

public class GeneratePlan extends CustomJavaAction<java.lang.Long> {
    private final IMendixObject settingsObj;
    private final String weekday;

    public GeneratePlan(IContext ctx, IMendixObject settingsObj, String weekday) {
        super(ctx);
        this.settingsObj = settingsObj;
        this.weekday = weekday == null ? "" : weekday;
    }

    // ---- one gate entry per truck visit --------------------------------------
    static final class Trip {
        String vendor, slot; LocalDate date; double offset; Set<String> bays = new LinkedHashSet<>();
        Map<String, Map<String, Double>> qtyByBayItem = new HashMap<>(); // bay -> item -> qty
    }

    @Override
    public java.lang.Long executeAction() throws Exception {
        IContext ctx = getContext();
        PlanSettings s = PlanSettings.initialize(ctx, settingsObj);
        LocalDate from = LocalDate.now().minusWeeks(s.getHistoryWeeks());
        DayOfWeek dow = weekday.isEmpty() ? null : DayOfWeek.valueOf(weekday.toUpperCase());

        // 1. build trips --------------------------------------------------------
        Map<String, Trip> trips = new HashMap<>();
        for (IMendixObject o : Core.retrieveXPathQuery(ctx,
                "//TripPlanning.GateEntry[SupplyDate >= '" + from + "']")) {
            GateEntry g = GateEntry.initialize(ctx, o);
            if (g.getGateIn() == null || g.getSupplyDate() == null || g.getSlot() == null) continue;
            LocalDate d = g.getSupplyDate().toInstant().atZone(ZoneId.systemDefault()).toLocalDate();
            if (dow != null && d.getDayOfWeek() != dow) continue;
            LocalDateTime in = LocalDateTime.ofInstant(g.getGateIn().toInstant(), ZoneId.systemDefault());
            String key = g.getTruckId() + "@" + in;        // a returning truck is a new trip
            Trip t = trips.computeIfAbsent(key, k -> new Trip());
            t.vendor = g.getVendor(); t.slot = g.getSlot(); t.date = d;
            LocalDateTime slotDt = d.atTime(LocalTime.parse(g.getSlot()));
            t.offset = Duration.between(slotDt, in).toMinutes();
            if (g.getBay() != null) {
                t.bays.add(g.getBay());
                double q = g.getReceivedQty() == null ? 0 : g.getReceivedQty().doubleValue();
                t.qtyByBayItem.computeIfAbsent(g.getBay(), k -> new HashMap<>())
                               .merge(g.getItem(), q, Double::sum);
            }
        }
        List<Trip> kept = trips.values().stream()
            .filter(t -> t.offset >= -s.getEarlyCapH() * 60 && t.offset <= s.getLateCapH() * 60)
            .collect(Collectors.toList());
        long allDays = kept.stream().map(t -> t.date).distinct().count();

        // standards + critical items
        Map<String, ItemStandard> std = new HashMap<>();
        for (IMendixObject o : Core.retrieveXPathQuery(ctx, "//TripPlanning.ItemStandard")) {
            ItemStandard is = ItemStandard.initialize(ctx, o); std.put(is.getItem(), is);
        }

        // slot-wide defaults
        Map<String, List<Double>> bySlot = new HashMap<>();
        for (Trip t : kept) bySlot.computeIfAbsent(t.slot, k -> new ArrayList<>()).add(t.offset);

        // 2. per vendor-slot ------------------------------------------------------
        Map<String, List<Trip>> groups = kept.stream()
            .collect(Collectors.groupingBy(t -> t.vendor + "|" + t.slot));
        int version = 1 + (int) Core.retrieveXPathQuery(ctx, "//TripPlanning.VendorSlotPlan")
                                    .stream().map(o -> VendorSlotPlan.initialize(ctx, o).getPlanVersion())
                                    .filter(Objects::nonNull).mapToInt(Integer::intValue).max().orElse(0);
        long written = 0;
        for (Map.Entry<String, List<Trip>> e : groups.entrySet()) {
            String vendor = e.getKey().split("\\|")[0], slot = e.getKey().split("\\|")[1];
            List<Trip> g = e.getValue();
            List<Double> off = g.stream().map(t -> t.offset).sorted().collect(Collectors.toList());
            long runDays = g.stream().map(t -> t.date).distinct().count();
            int runsPct = (int) Math.round(100.0 * runDays / Math.max(allDays, 1));
            if (runsPct < s.getMinRunPct()) continue;

            boolean own = off.size() >= s.getMinTrucks();
            List<Double> src = own ? off : bySlot.get(slot).stream().sorted().collect(Collectors.toList());
            double a = pct(src, s.getQStart().doubleValue());
            double b = pct(src, s.getQStop().doubleValue());
            int round = s.getRoundMin();
            int safe = (int) (Math.ceil(a / round) * round);
            int hard = (int) (Math.ceil((b + s.getGraceMin()) / round) * round);

            VendorSlotPlan p = new VendorSlotPlan(ctx);
            p.setVendor(vendor); p.setSlot(slot); p.setWeekday(weekday.isEmpty() ? "All" : weekday);
            p.setSafeStartOffset(safe); p.setHardStopOffset(hard);
            p.setSafeStart(toDate(LocalDate.now().atTime(LocalTime.parse(slot)).plusMinutes(safe)));
            p.setHardStop(toDate(LocalDate.now().atTime(LocalTime.parse(slot)).plusMinutes(hard)));
            p.setBasis(own ? "Vendor history" : "Slot default (few trucks)");
            p.setTrucksInHistory(off.size()); p.setRunsPct(runsPct);
            p.setPlanVersion(version); p.setGeneratedOn(new Date()); p.setIsActive(true);
            p.commit();
            written++;

            // 3. bay list: >=10% of trips, top 5, critical bays first then share ----
            Map<String, Long> visits = new HashMap<>();
            Map<String, Boolean> critBay = new HashMap<>();
            for (Trip t : g) for (String bay : t.bays) {
                visits.merge(bay, 1L, Long::sum);
                boolean crit = t.qtyByBayItem.getOrDefault(bay, Map.of()).keySet().stream()
                    .anyMatch(it -> std.containsKey(it) && Boolean.TRUE.equals(std.get(it).getIsCritical()));
                if (crit) critBay.put(bay, true);
            }
            List<Map.Entry<String, Long>> bays = visits.entrySet().stream()
                .filter(x -> 100.0 * x.getValue() / g.size() >= 10)
                .sorted(Comparator.<Map.Entry<String, Long>>comparingInt(
                            x -> critBay.containsKey(x.getKey()) ? 0 : 1)
                        .thenComparing(x -> -x.getValue()))
                .limit(5).collect(Collectors.toList());

            int seq = 0;
            for (Map.Entry<String, Long> bv : bays) {
                String bay = bv.getKey();
                // 4. stop minutes from standards, P75 of past days + allowance + fixed ----
                List<Double> dayMins = new ArrayList<>(); double qStd = 0, qAll = 0;
                for (Trip t : g) {
                    Map<String, Double> items = t.qtyByBayItem.get(bay);
                    if (items == null) continue;
                    double m = 0;
                    for (Map.Entry<String, Double> it : items.entrySet()) {
                        ItemStandard is = std.get(it.getKey()); double q = it.getValue(); qAll += q;
                        if (is == null || is.getStdMinutes() == null) continue;
                        double tm = is.getStdMinutes().doubleValue();
                        double ps = is.getPackSize() == null ? 0 : is.getPackSize().doubleValue();
                        String basis = is.getBasis() == null ? "per pack" : is.getBasis();
                        if (basis.equals("per piece")) m += q * tm;
                        else if (basis.equals("per stop")) m += q > 0 ? tm : 0;
                        else if (ps > 0) m += Math.ceil(q / ps) * tm; else continue;
                        qStd += q;
                    }
                    dayMins.add(m);
                }
                int mins; String mb;
                if (qAll > 0 && qStd / qAll >= 0.5 && !dayMins.isEmpty()) {
                    double raw = pct(dayMins.stream().sorted().collect(Collectors.toList()),
                                     s.getQStopMinutes().doubleValue());
                    mins = (int) (Math.ceil((raw * (1 + s.getAllowancePct() / 100.0) + s.getFixedMin()) / 5) * 5);
                    mb = "Your standard";
                } else { mins = s.getManualMin(); mb = "Manual (no standard)"; }

                PlanStop ps = new PlanStop(ctx);
                ps.setSequence(++seq); ps.setStopMinutes(mins); ps.setMinutesBasis(mb);
                ps.setIsCritical(critBay.containsKey(bay));
                ps.setSharePct((int) Math.round(100.0 * bv.getValue() / g.size()));
                ps.setPlanStop_VendorSlotPlan(p);
                List<IMendixObject> bo = Core.retrieveXPathQuery(ctx,
                    "//TripPlanning.Bay[Code = '" + bay.replace("'", "''") + "']");
                if (!bo.isEmpty()) ps.setPlanStop_Bay(Bay.initialize(ctx, bo.get(0)));
                ps.commit();
            }
        }
        return written;
    }

    /** Linear-interpolated percentile of a sorted list (same as pandas default). */
    static double pct(List<Double> sorted, double q) {
        if (sorted.isEmpty()) return 0;
        double pos = q * (sorted.size() - 1);
        int lo = (int) Math.floor(pos), hi = (int) Math.ceil(pos);
        return sorted.get(lo) + (sorted.get(hi) - sorted.get(lo)) * (pos - lo);
    }

    static Date toDate(LocalDateTime t) {
        return Date.from(t.atZone(ZoneId.systemDefault()).toInstant());
    }

    @Override public String toString() { return "GeneratePlan"; }
}
