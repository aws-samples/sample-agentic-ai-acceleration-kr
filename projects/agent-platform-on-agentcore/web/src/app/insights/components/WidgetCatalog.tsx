"use client";

import { useState } from "react";
import { MoreVertical, RotateCcw } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Button } from "@/components/ui/button";
import {
  DEFAULT_LAYOUT,
  toggleWidget,
  setSpan,
  WIDGET_IDS,
} from "../layoutModel.mjs";
import type { WidgetId, WidgetConfig } from "../widgetRegistry";

interface Widget {
  id: string;
  span: string;
  visible: boolean;
}

interface WidgetCatalogProps {
  widgets: Widget[];
  registry: Record<WidgetId, WidgetConfig>;
  onLayoutChange: (newWidgets: Widget[]) => void;
}

export function WidgetCatalog({
  widgets,
  registry,
  onLayoutChange,
}: WidgetCatalogProps) {
  const [open, setOpen] = useState(false);

  const handleToggle = (id: string) => {
    onLayoutChange(toggleWidget(widgets, id));
  };

  const handleSetSpan = (id: string, span: "half" | "full") => {
    onLayoutChange(setSpan(widgets, id, span));
  };

  const handleReset = () => {
    onLayoutChange([...DEFAULT_LAYOUT.widgets]);
    setOpen(false);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="sm">
          <MoreVertical className="size-4" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-64" align="end">
        <div className="space-y-4">
          <div className="text-sm font-semibold">위젯 설정</div>

          <div className="space-y-2 max-h-64 overflow-y-auto">
            {WIDGET_IDS.map((id) => {
              const widget = widgets.find((w) => w.id === id);
              const config = registry[id as WidgetId];
              if (!widget) return null;

              return (
                <div key={id} className="space-y-1.5 border-b pb-2">
                  <div className="flex items-center justify-between">
                    <label className="text-xs font-medium cursor-pointer">
                      <input
                        type="checkbox"
                        checked={widget.visible}
                        onChange={() => handleToggle(id)}
                        className="mr-2"
                      />
                      {config.title}
                    </label>
                  </div>
                  {widget.visible && (
                    <div className="flex gap-1 pl-6">
                      <Button
                        size="sm"
                        variant={widget.span === "half" ? "default" : "ghost"}
                        onClick={() => handleSetSpan(id, "half")}
                        className="h-6 px-2 text-xs"
                      >
                        반
                      </Button>
                      <Button
                        size="sm"
                        variant={widget.span === "full" ? "default" : "ghost"}
                        onClick={() => handleSetSpan(id, "full")}
                        className="h-6 px-2 text-xs"
                      >
                        전
                      </Button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          <Button
            variant="outline"
            size="sm"
            onClick={handleReset}
            className="w-full"
          >
            <RotateCcw className="size-3.5 mr-2" />
            기본값으로
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
