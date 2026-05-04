"use client";

import { AgGridReact } from "ag-grid-react";
import {
  ClientSideRowModelModule,
  ModuleRegistry,
  type CellClassParams,
  type CellClassRules,
  type CellValueChangedEvent,
  type ColDef,
  type ICellEditorParams,
} from "ag-grid-community";
import { useMemo, useRef } from "react";

import type { QcSheetPayload } from "@/lib/types";

ModuleRegistry.registerModules([ClientSideRowModelModule]);


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
  } else if (field === "score" || field === "ML Matches Lookup") {
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
  const containerRef = useRef<HTMLDivElement | null>(null);

  const colDefs: ColDef[] = useMemo(() => {
    return payload.columns
      .filter((c) => c.field !== "_row_id")
      .map<ColDef>((c) => ({
        field: c.field,
        headerName: c.header,
        editable: c.editable,
        sortable: false,
        filter: true,
        resizable: true,
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

  return (
    <div
      ref={containerRef}
      className="ag-theme-quartz w-full"
      style={{ height: 520 }}
    >
      <AgGridReact
        rowData={payload.rows as Record<string, unknown>[]}
        columnDefs={colDefs}
        context={context}
        getRowId={(p) => String((p.data as Record<string, unknown>)._row_id ?? "")}
        animateRows={false}
        pagination={true}
        paginationPageSize={50}
        paginationPageSizeSelector={[25, 50, 100, 200]}
        onCellValueChanged={onCellValueChanged}
        singleClickEdit={false}
        stopEditingWhenCellsLoseFocus={true}
      />
    </div>
  );
}
