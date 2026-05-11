"use client";

// ag-grid wrapper for one QC lookup sheet. Edited cells tint via
// cellClassRules that compare live rowData against payload.original_values
// (passed through grid context so we don't rebuild colDefs per keystroke).
//
// ag-grid-community v32 (package install) auto-registers community modules.
// Calling ModuleRegistry.registerModules from the package surface triggers
// AG Grid's "mixing modules and packages" warning, so we don't.

import { AgGridReact } from "ag-grid-react";
import type {
  CellClassParams,
  CellClassRules,
  CellValueChangedEvent,
  ColDef,
  FirstDataRenderedEvent,
  ICellEditorParams,
} from "ag-grid-community";
import { useMemo } from "react";

import type { QcSheetPayload } from "@/lib/types";


function classRulesFor(field: string, attr: string): CellClassRules {
  // Whole-row tint when the editable attribute differs from the original.
  // The "original_values" map travels with the payload so we can detect
  // edits client-side without a server round-trip on every keystroke.
  const editedRule = (params: CellClassParams) => {
    const orig = (params.context?.originalValues ?? {}) as Record<string, string>;
    const rowId = params.data?._row_id as string | undefined;
    if (!rowId) return false;
    return String(params.data?.[attr] ?? "") !== String(orig[rowId] ?? "");
  };

  const rules: CellClassRules = { "row-edited": editedRule };

  if (field === "QC Priority") {
    rules["cell-high-priority"] = (p: CellClassParams) => p.value === "HIGH";
  } else if (field === "score") {
    rules["cell-low-score-no-ml"] = (p: CellClassParams) => {
      const flags = (p.context?.rowFlags ?? {}) as Record<string, string[]>;
      const rowId = p.data?._row_id as string | undefined;
      return !!rowId && (flags[rowId] ?? []).includes("low_score_no_ml");
    };
  } else if (field === "Note") {
    rules["cell-note"] = (p: CellClassParams) =>
      Boolean(p.value && p.value !== "" && p.value !== "nan");
  }

  return rules;
}


export function QcGrid({
  payload,
  onEdit,
}: {
  payload: QcSheetPayload;
  onEdit: (rowId: string, value: string) => void;
}) {
  const colDefs: ColDef[] = useMemo(() => {
    return payload.columns
      .filter((c) => c.field !== "_row_id")
      .map<ColDef>((c) => ({
        field: c.field,
        headerName: c.header,
        editable: c.editable,
        // Editable column: light brand tint on the header (visual cue for the
        // analyst's primary interaction column) + chevron in every cell so
        // the dropdown affordance is visible without a double-click discovery.
        headerClass: c.editable ? "ag-header-editable" : undefined,
        cellClass: c.editable ? "cell-editable-select" : undefined,
        // Editable column needs slightly more room for value + chevron.
        maxWidth: c.editable ? 280 : undefined,
        cellEditor: c.editable ? "agSelectCellEditor" : undefined,
        cellEditorParams: c.editable
          ? ({ values: payload.attribute_options } as Partial<ICellEditorParams>)
          : undefined,
        cellClassRules: classRulesFor(c.field, payload.attribute),
      }));
  }, [payload]);

  const context = useMemo(
    () => ({
      originalValues: payload.original_values,
      rowFlags: payload.row_flags,
    }),
    [payload],
  );

  function onCellValueChanged(e: CellValueChangedEvent) {
    if (e.colDef.field !== payload.attribute) return;
    const rowId = e.data?._row_id as string | undefined;
    if (!rowId) return;
    onEdit(rowId, String(e.newValue ?? ""));
  }

  // Size each column to its widest visible cell on first render.  Mirrors
  // the Streamlit version's "fit columns to content" behaviour.  The
  // defaultColDef minWidth/maxWidth caps still apply, so a single very
  // long DESCRIPTION row can't blow the layout out.
  function onFirstDataRendered(e: FirstDataRenderedEvent) {
    e.api.autoSizeAllColumns();
  }

  return (
    <div className="ag-theme-quartz w-full" style={{ height: 520 }}>
      <AgGridReact
        rowData={payload.rows as Record<string, unknown>[]}
        columnDefs={colDefs}
        defaultColDef={{
          minWidth: 90,
          maxWidth: 220,
          sortable: false,
          filter: true,
          resizable: true,
        }}
        context={context}
        getRowId={(p) => String((p.data as Record<string, unknown>)._row_id ?? "")}
        animateRows={false}
        pagination={true}
        paginationPageSize={50}
        paginationPageSizeSelector={[25, 50, 100, 200]}
        onCellValueChanged={onCellValueChanged}
        onFirstDataRendered={onFirstDataRendered}
        singleClickEdit={false}
        stopEditingWhenCellsLoseFocus={true}
      />
    </div>
  );
}
