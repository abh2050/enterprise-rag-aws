"""Tiny draw.io (mxGraph) writer following the aws-architecture-diagram skill conventions.

Coordinates passed to the builder are ABSOLUTE; children of containers are converted to
parent-relative geometry on output. Icon names are verified against
.claude/skills/aws-architecture-diagram/references/*.md (never guessed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.sax.saxutils import escape

# Pattern 1: service-level (resourceIcon frame, strokeColor=#ffffff)
SERVICE = {
    "cloudfront": "#8C4FFF",
    "waf": "#DD344C",
    "shield": "#DD344C",
    "key_management_service": "#DD344C",
    "secrets_manager": "#DD344C",
    "network_firewall": "#DD344C",
    "s3": "#7AA116",
    "backup": "#7AA116",
    "ecs": "#ED7100",
    "fargate": "#ED7100",
    "ecr": "#ED7100",
    "dynamodb": "#C925D1",
    "rds": "#C925D1",
    "elasticache": "#C925D1",
    "eks": "#ED7100",
    "sns": "#E7157B",
    "sqs": "#E7157B",
    "eventbridge": "#E7157B",
    "step_functions": "#E7157B",
    "cloudwatch_2": "#E7157B",
    "cloudtrail": "#E7157B",
    "organizations": "#E7157B",
    "elasticsearch_service": "#8C4FFF",
    "bedrock": "#01A88D",
    "textract": "#01A88D",
    "codebuild": "#C925D1",
    "xray": "#C925D1",
}
# Pattern 2: resource-level (standalone shape, strokeColor=none)
RESOURCE = {
    "application_load_balancer": "#8C4FFF",
    "endpoints": "#8C4FFF",
    "nat_gateway": "#8C4FFF",
    "internet_gateway": "#8C4FFF",
    "eventbridge_pipes": "#E7157B",
    "eventbridge_scheduler": "#E7157B",
    "alarm": "#E7157B",
    "event_time_based": "#E7157B",
    "role": "#DD344C",
    "permissions": "#DD344C",
    "bucket": "#7AA116",
    "container_1": "#ED7100",
}
# General resources (standalone, dark)
GENERAL = {
    "users",
    "user",
    "client",
    "internet",
    "internet_alt1",
    "document",
    "documents",
    "source_code",
    "saml_token",
    "servers",
    "traditional_server",
    "office_building",
    "gear",
    "generic_firewall",
    "sdk",
}

GROUPS = {
    "cloud": ("group_aws_cloud_alt", "#232F3E", "#232F3E"),
    "region": ("group_region", "#00A4A6", "#147EBA"),
    "az": ("group_availability_zone", "#007FAA", "#007FAA"),
    "vpc": ("group_vpc", "#8C4FFF", "#8C4FFF"),
    "private": ("group_private_subnet", "#147EBA", "#147EBA"),
    "public": ("group_public_subnet", "#248814", "#248814"),
    "sg": ("group_security_group", "#DD344C", "#DD344C"),
    "account": ("group_account", "#CD2264", "#CD2264"),
    "onprem": ("group_on_premise", "#5A6C86", "#5A6C86"),
    "corp": ("group_corporate_data_center", "#7D8998", "#5A6C86"),
    "sfn": ("group_aws_step_functions_workflow", "#CD2264", "#CD2264"),
}

SIDES = {"r": (1, 0.5), "l": (0, 0.5), "t": (0.5, 0), "b": (0.5, 1)}
EDGE_KIND = {
    "solid": "",
    "async": "dashed=1;",
    "error": "dashed=1;strokeColor=#DD344C;fontColor=#DD344C;",
    "ok": "strokeColor=#1D8102;fontColor=#1D8102;",
    "muted": "dashed=1;strokeColor=#879196;fontColor=#545B64;",
}
LABEL = "html=1;fontSize=12;fontColor=#232F3E;labelBackgroundColor=#F5F5F5;"
LABEL_POS = {
    "b": "verticalLabelPosition=bottom;verticalAlign=top;align=center;",
    "t": "verticalLabelPosition=top;verticalAlign=bottom;align=center;",
    "r": "labelPosition=right;verticalLabelPosition=middle;align=left;verticalAlign=middle;spacingLeft=4;",
    "l": "labelPosition=left;verticalLabelPosition=middle;align=right;verticalAlign=middle;spacingRight=4;",
}


@dataclass
class Cell:
    id: str
    value: str
    style: str
    x: float = 0
    y: float = 0
    w: float = 0
    h: float = 0
    parent: str = "1"
    edge: bool = False
    source: str = ""
    target: str = ""
    points: tuple[tuple[float, float], ...] = ()


@dataclass
class Page:
    id: str
    name: str
    width: int = 2400
    height: int = 1400
    cells: list[Cell] = field(default_factory=list)
    _abs: dict[str, tuple[float, float]] = field(default_factory=dict)
    _n: int = 0

    def __post_init__(self) -> None:
        self.cells.append(
            Cell(
                "bg",
                "",
                "rounded=0;whiteSpace=wrap;html=1;fillColor=#F5F5F5;strokeColor=none;",
                0,
                0,
                self.width,
                self.height,
            )
        )

    def _uid(self, prefix: str) -> str:
        self._n += 1
        return f"{self.id}-{prefix}{self._n}"

    def _add(self, c: Cell) -> str:
        assert c.id not in self._abs, f"duplicate id {c.id}"
        self._abs[c.id] = (c.x, c.y)
        self.cells.append(c)
        return c.id

    # ---- vertices -------------------------------------------------------------------------
    def title(self, title: str, subtitle: str) -> None:
        self._add(
            Cell(
                self._uid("title"),
                f"<b style='font-size:20px'>{title}</b><br>{subtitle}",
                "text;html=1;align=left;verticalAlign=top;whiteSpace=wrap;rounded=0;fontSize=13;"
                "spacing=8;fontColor=#232F3E;",
                30,
                20,
                1500,
                70,
            )
        )

    def icon(
        self,
        id: str,
        name: str,
        label: str,
        x: float,
        y: float,
        parent: str = "1",
        size: int = 78,
        lp: str = "b",
    ) -> str:
        lab = LABEL + LABEL_POS[lp]
        if name in SERVICE:
            style = (
                f"sketch=0;outlineConnect=0;{lab}fillColor={SERVICE[name]};strokeColor=#ffffff;dashed=0;"
                f"aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{name};"
            )
        elif name in RESOURCE:
            style = (
                f"sketch=0;outlineConnect=0;{lab}fillColor={RESOURCE[name]};strokeColor=none;dashed=0;"
                f"aspect=fixed;shape=mxgraph.aws4.{name};"
            )
        elif name in GENERAL:
            style = (
                f"sketch=0;outlineConnect=0;{lab}fillColor=#232F3D;strokeColor=none;dashed=0;"
                f"aspect=fixed;shape=mxgraph.aws4.{name};"
            )
        else:
            raise KeyError(f"unverified icon {name}")
        return self._add(Cell(id, label, style, x, y, size, size, parent))

    def box(
        self,
        id: str,
        label: str,
        x: float,
        y: float,
        w: float = 210,
        h: float = 80,
        parent: str = "1",
        fill: str = "#FFFFFF",
        stroke: str = "#545B64",
        font: int = 12,
        align: str = "center",
    ) -> str:
        style = (
            f"rounded=1;arcSize=8;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
            f"fontSize={font};fontColor=#232F3E;align={align};spacing=6;"
        )
        return self._add(Cell(id, label, style, x, y, w, h, parent))

    def note(
        self, label: str, x: float, y: float, w: float, h: float, parent: str = "1", font: int = 12
    ) -> str:
        style = (
            f"text;html=1;align=left;verticalAlign=top;whiteSpace=wrap;rounded=1;fontSize={font};"
            "fontColor=#232F3E;spacing=10;fillColor=#FFFFFF;strokeColor=#D5DBDB;arcSize=4;"
        )
        return self._add(Cell(self._uid("note"), label, style, x, y, w, h, parent))

    def group(
        self, id: str, kind: str, label: str, x: float, y: float, w: float, h: float, parent: str = "1"
    ) -> str:
        gr, stroke, font = GROUPS[kind]
        style = (
            "points=[];outlineConnect=0;gradientColor=none;html=1;whiteSpace=wrap;fontSize=12;fontStyle=1;"
            "container=1;dropTarget=1;pointerEvents=0;collapsible=0;recursiveResize=0;"
            f"shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.{gr};strokeColor={stroke};fillColor=none;"
            f"verticalAlign=top;align=left;spacingLeft=30;fontColor={font};dashed=0;"
        )
        return self._add(Cell(id, label, style, x, y, w, h, parent))

    def dgroup(
        self,
        id: str,
        label: str,
        x: float,
        y: float,
        w: float,
        h: float,
        parent: str = "1",
        color: str = "#5A6C86",
    ) -> str:
        style = (
            "whiteSpace=wrap;html=1;fillColor=none;dashed=1;dashPattern=8 8;container=1;dropTarget=1;"
            f"collapsible=0;recursiveResize=0;strokeColor={color};fontColor={color};verticalAlign=top;"
            "align=left;spacingLeft=10;spacingTop=4;fontSize=12;fontStyle=1;"
        )
        return self._add(Cell(id, label, style, x, y, w, h, parent))

    # ---- edges ----------------------------------------------------------------------------
    def edge(
        self,
        src: str,
        dst: str,
        label: str = "",
        kind: str = "solid",
        exit: str = "r",
        entry: str = "l",
        ex: tuple[float, float] | None = None,
        en: tuple[float, float] | None = None,
        both: bool = False,
        pts: tuple[tuple[float, float], ...] = (),
    ) -> str:
        sx, sy = ex or SIDES[exit]
        tx, ty = en or SIDES[entry]
        vertical = exit in ("t", "b") and ex is None
        lab = "align=right;" if vertical else "verticalAlign=bottom;"
        style = (
            "edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;jettySize=auto;html=1;strokeWidth=2;"
            f"strokeColor=#545B64;fontColor=#232F3E;labelBackgroundColor=#F5F5F5;fontSize=11;{lab}"
            f"exitX={sx};exitY={sy};exitDx=0;exitDy=0;entryX={tx};entryY={ty};entryDx=0;entryDy=0;"
            f"endArrow=block;endFill=1;{'startArrow=block;startFill=1;' if both else ''}{EDGE_KIND[kind]}"
        )
        c = Cell(self._uid("e"), label, style, edge=True, source=src, target=dst, points=pts)
        self.cells.append(c)
        return c.id

    # ---- output ---------------------------------------------------------------------------
    def xml(self) -> str:
        out = [
            f'  <diagram id="{self.id}" name="{escape(self.name)}">',
            f'    <mxGraphModel dx="{self.width + 400}" dy="{self.height + 200}" grid="1" gridSize="10" '
            'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
            f'pageWidth="{self.width}" pageHeight="{self.height}" math="0" shadow="0">',
            "      <root>",
            '        <mxCell id="0" />',
            '        <mxCell id="1" parent="0" />',
        ]
        ordered = (
            self.cells[:1] + [c for c in self.cells if c.edge] + [c for c in self.cells[1:] if not c.edge]
        )
        for c in ordered:
            q = lambda s: escape(s, {'"': "&quot;"})  # noqa: E731
            val = f' value="{q(c.value)}"' if c.value else ""
            if c.edge:
                out.append(
                    f'        <mxCell id="{c.id}"{val} style="{q(c.style)}" edge="1" parent="1" '
                    f'source="{c.source}" target="{c.target}">'
                )
                if c.points:
                    out.append('          <mxGeometry relative="1" as="geometry">')
                    out.append('            <Array as="points">')
                    out += [f'              <mxPoint x="{x:g}" y="{y:g}" />' for x, y in c.points]
                    out.append("            </Array>")
                    out.append("          </mxGeometry>")
                else:
                    out.append('          <mxGeometry relative="1" as="geometry" />')
            else:
                px, py = self._abs.get(c.parent, (0, 0))
                out.append(
                    f'        <mxCell id="{c.id}"{val} style="{q(c.style)}" vertex="1" parent="{c.parent}">'
                )
                out.append(
                    f'          <mxGeometry x="{c.x - px:g}" y="{c.y - py:g}" width="{c.w:g}" '
                    f'height="{c.h:g}" as="geometry" />'
                )
            out.append("        </mxCell>")
        ids = {c.id for c in self.cells}
        for c in self.cells:
            if c.edge:
                assert c.source in ids and c.target in ids, f"dangling edge {c.id}"
        out += ["      </root>", "    </mxGraphModel>", "  </diagram>"]
        return "\n".join(out)


def write(path: str, *pages: Page) -> None:
    body = "\n".join(p.xml() for p in pages)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f'<mxfile host="app.diagrams.net">\n{body}\n</mxfile>\n')
