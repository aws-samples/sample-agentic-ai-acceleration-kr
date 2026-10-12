"use client";

import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  DragEndEvent,
} from "@dnd-kit/core";
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useSortable } from "@dnd-kit/sortable";
import { GripVertical, BarChart3 } from "lucide-react";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/app/components/PageHeader";
import { spanClass, visibleWidgets } from "../layoutModel.mjs";
import type { WidgetId, WidgetConfig } from "../widgetRegistry";

interface Widget {
  id: string;
  span: string;
  visible: boolean;
}

interface WidgetGridProps {
  widgets: Widget[];
  registry: Record<WidgetId, WidgetConfig>;
  onLayoutChange: (newWidgets: Widget[]) => void;
}

function SortableWidgetItem({
  id,
  widget,
  config,
}: {
  id: string;
  widget: Widget;
  config: WidgetConfig;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({
    id,
    strategy: verticalListSortingStrategy,
  });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  };

  const content = config.render();
  if (!content) {
    return null;
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={cn(
        "rounded-lg border bg-card shadow-sm",
        spanClass(widget.span),
      )}
    >
      <div
        className="flex items-center gap-2 border-b px-4 py-3"
        {...attributes}
        {...listeners}
      >
        <GripVertical className="size-4 cursor-grab text-muted-foreground active:cursor-grabbing" />
        <h3 className="text-sm font-semibold">{config.title}</h3>
      </div>
      {/*
       * `@container` so the plots inside size against this card, not the viewport.
       * A widget the reader dragged down to `half` is ~600px wide on a 1600px
       * screen, and `lg:grid-cols-2` inside it read the screen: two plots at 260px
       * each, side by side, in a card that had room for one.
       */}
      <div className="@container p-4">{content}</div>
    </div>
  );
}

export function WidgetGrid({
  widgets,
  registry,
  onLayoutChange,
}: WidgetGridProps) {
  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );

  const visible = visibleWidgets(widgets);

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (over && active.id !== over.id) {
      const oldIndex = visible.findIndex((w: Widget) => w.id === active.id);
      const newIndex = visible.findIndex((w: Widget) => w.id === over.id);
      const reordered = arrayMove(visible, oldIndex, newIndex) as Widget[];

      // Reconstruct the full widgets array with reordered visible ones in place
      const newWidgets = [...widgets];
      let visibleIndex = 0;
      for (let i = 0; i < newWidgets.length; i++) {
        if (newWidgets[i].visible) {
          newWidgets[i] = reordered[visibleIndex++];
        }
      }
      onLayoutChange(newWidgets);
    }
  };

  if (visible.length === 0) {
    return (
      <EmptyState
        icon={BarChart3}
        title="모든 위젯이 숨겨져 있습니다"
        description="설정에서 위젯을 다시 표시하세요."
      />
    );
  }

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      onDragEnd={handleDragEnd}
    >
      <SortableContext
        items={visible.map((w: Widget) => w.id)}
        strategy={verticalListSortingStrategy}
      >
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {visible.map((widget: Widget) => (
            <SortableWidgetItem
              key={widget.id}
              id={widget.id}
              widget={widget}
              config={registry[widget.id as WidgetId]}
            />
          ))}
        </div>
      </SortableContext>
    </DndContext>
  );
}
