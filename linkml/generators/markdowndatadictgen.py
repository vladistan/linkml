import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Union

import pydantic
from py_markdown_table.markdown_table import markdown_table

import click
from jsonasobj2 import JsonObj, values
from linkml_runtime.linkml_model.meta import (
    ClassDefinition,
    ClassDefinitionName,
    Element,
    EnumDefinition,
    SlotDefinition,
    SubsetDefinition,
    TypeDefinition,
)
from linkml_runtime.utils.formatutils import be, camelcase, underscore

from linkml._version import __version__
from linkml.generators.erdiagramgen import ERDiagramGenerator
from linkml.utils.generator import Generator, shared_arguments
from linkml.utils.typereferences import References


class ClassRelationship(pydantic.BaseModel):
    base: str
    derived: str

    def __str__(self) -> str:
        return f"{self.base} <|-- {self.derived}"

    def __hash__(self):
        return hash((self.base, self.derived))


class ClassDiagram(pydantic.BaseModel):
    relationships: Set[ClassRelationship] = set({})

    def add_relationship(self, base, derived):
        self.relationships |= {ClassRelationship(base=base, derived=derived)}

    def __str__(self) -> str:
        rels = "\n".join([str(r) for r in self.relationships])
        return f"classDiagram\n{rels}\n"


@dataclass
class MarkdownDataDictGen(Generator):
    """
    Generates single page data dictionary for a LinkML schema

    All schema elements (class, slot, type, enum) are placed into a single document, with a ERD diagram at the top

    The markdown is suitable to create discussion documents used while developing a new data model
    """

    # ClassVars
    generatorname = os.path.basename(__file__)
    generatorversion = "0.0.1"
    directory_output = True
    valid_formats = ["md"]
    visit_all_class_slots = False
    uses_schemaloader = True

    # ObjectVars
    output: Optional[str] = None
    image_directory: Optional[str] = None
    classes: Set[ClassDefinitionName] = None
    image_dir: bool = False
    index_file: str = "index.md"
    noimages: bool = False
    noyuml: bool = False
    no_types_dir: bool = False
    warn_on_exist: bool = False
    gen_classes: Optional[Set[ClassDefinitionName]] = None
    gen_classes_neighborhood: Optional[References] = None

    schema_classes = Set[ClassDefinition]

    def visit_schema(
            self,
            output: str = None,
            classes: Set[ClassDefinitionName] = None,
            image_dir: bool = False,
            index_file: str = "index.md",
            noimages: bool = False,
            **_,
    ) -> str:

        self.gen_classes = classes if classes else []
        for cls in self.gen_classes:
            if cls not in self.schema.classes:
                raise ValueError("Unknown class name: {cls}")

        if self.gen_classes:
            self.gen_classes_neighborhood = self.neighborhood(list(self.gen_classes))

        self.output = output

        items: list[str] = []
        items.append(self.frontmatter(f"{self.schema.name.upper()}"))
        items.append(
            self.para(
                f"**metamodel version:** {self.schema.metamodel_version}\n\n**version:** {self.schema.version}"
            )
        )

        items.append(self.para(be(self.schema.description)))

        # Class
        items.append(self.header(2, "Class Diagram"))
        items.append(f"```mermaid\n{self.full_class_diagram()}\n```")

        erd_gen = ERDiagramGenerator(self.schema_location, exclude_attributes=False)
        items.append(self.header(2, "ERD Diagram"))
        items.append(erd_gen.serialize())

        items.append(self.header(2, "Classes"))
        for cls in sorted(self.schema.classes.values(),
                          key=lambda c: ('0' if self.get_class_children(c.name) else '1') + c.name):

            # Skip Mixins
            if cls.mixin:
                continue
            items.extend(self.describe_class(cls))
            items.append("\n\n")

            # List all of the slots acquired through mixing
            # mixed_in_classes = set()
            # for mixin in cls.mixins:
            #      mixed_in_classes.add(mixin)
            # mixed_in_classes.update(set(self.ancestors(self.schema.classes[mixin])))
            # for slot in slot_list:
            #     mixers = set(slot.domain_of).intersection(mixed_in_classes)
            # for mixer in mixers:
            #     items.append(self.header(3, "Mixed in from " + mixer + ":"))
            # items.append(self.slot_field(cls, slot))

            # items.append(self.(f"Range: {self.class_type_link(slot.range)}", level=1))
            #  for example in slot.examples:
            #      items.append(
            #          self.(
            #              f'Example: {getattr(example, "value", " ")} {getattr(example, "description", " ")}',
            #              level=1,
            #          )
            #      )

        mixins = [cls for cls in sorted(self.schema.classes.values(), key=lambda c: c.name) if cls.mixin]
        if mixins:
            items.append(self.header(2, "Mixins"))
            for cls in mixins:
                items.extend(self.describe_class(cls))

        enums = sorted(self.schema.enums.values(), key=lambda e: e.name)
        if enums:
            items.append(self.header(2, "Enums"))
            for en in enums:
                items.extend(self.describe_enum(en))

        items = [i for i in items if i is not None]
        out = "\n".join(items) + "\n"
        out = pad_heading(out)
        return out

    def local_class_diagram(self, cls):
        class_diagram = ClassDiagram()
        visited = set({})

        def add_ancestor(this_class):
            if this_class.is_a:
                class_diagram.add_relationship(base=cls.is_a, derived=this_class.name)
                add_ancestor(self.schema.classes[cls.is_a])

        add_ancestor(cls)
        children = self.get_class_children(cls.name)
        for child in children:
            class_diagram.add_relationship(base=cls.name, derived=child)
            visited.add(child)
        return class_diagram

    def full_class_diagram(self):
        class_diagram = ClassDiagram()
        for cls in self.schema.classes.values():
            if cls.is_a:
                class_diagram.add_relationship(base=cls.is_a, derived=cls.name)
        return class_diagram

    def describe_enum(self, enu):
        items: list[str] = []

        class_curi = self.namespaces.uri_or_curie_for(str(self.namespaces._base), camelcase(enu.name))
        class_uri = self.namespaces.uri_for(class_curi)

        items.append(self.element_header(enu, enu.name, class_curi, class_uri))
        attributes = []

        for value in enu.permissible_values.values():
            attributes.append({
                'Text': value.text,
                'Meaning:': value.meaning,
                'Description': value.description if value.description else ''
            })
        if len(attributes) > 0:
            items.append(markdown_table(attributes).set_params(
                quote=False, row_sep='markdown').get_markdown())

        if enu.name in self.synopsis.enumrefs:
            enu_refs = self.synopsis.enumrefs.get(enu.name)
            if enu_refs:
                items.append(self.header(4, "Used by"))
                for sn in enu_refs.slotrefs:
                    slot = self.schema.slots[sn]
                    for domain in slot.domain_of:
                        items.append(
                            self.bullet(
                                f" **{self.class_link(domain)}** "
                                f"*{self.slot_link(slot, add_subset=False)}*{self.predicate_cardinality(slot)} "
                            )
                        )
        return items

    def describe_class(self, cls):
        items: list[str] = []
        class_curi = self.namespaces.uri_or_curie_for(str(self.namespaces._base), camelcase(cls.name))
        class_uri = self.namespaces.uri_for(class_curi)

        class_refs = self.synopsis.classrefs.get(cls.name)
        has_references_to = class_refs is not None and bool(class_refs.slotrefs)
        children = self.get_class_children(cls.name)

        items.append(self.element_header(cls, cls.name, class_curi, class_uri))

        range_classes = []
        for slot_name in cls.slots:
            slot = self.schema.slots[slot_name]
            if slot.range in self.schema.classes:
                range_classes.append(slot.range)

        if range_classes or has_references_to and not cls.mixin:
            erd_gen = ERDiagramGenerator(self.schema_location, exclude_attributes=True, structural=True)
            small_diag = erd_gen.serialize_classes([cls.name], follow_references=False, max_hops=0)
            items.append(small_diag)
        elif children:
            items.append(self.header(4, "Local class diagram"))
            items.append(f"```mermaid\n{self.local_class_diagram(cls)}\n```")

        if cls.id_prefixes:
            items.append(self.header(3, "Identifier prefixes"))
            for p in cls.id_prefixes:
                items.append(self.bullet(f"{p}"))

        # List all the slots that directly belong to the class
        items.append(self.header(4, "Attributes"))
        items.append(self.slots_table(cls))

        if cls.is_a is not None:
            items.append(self.header(4, "Parents"))
            items.append(self.bullet(f"{self.class_link(cls.is_a, use_desc=True)}"))

        ## Children
        if children:
            items.append(self.header(4, "Children"))
            self.list_children(children, items)

        if cls.mixins:
            items.append(self.header(4, "Uses"))
            for mixin in cls.mixins:
                items.append(self.bullet(f" mixin: {self.class_link(mixin, use_desc=True)}"))

        if cls.mixin:
            cls_refs = self.synopsis.mixinrefs.get(cls.name)
            if cls_refs:
                items.append(self.header(4, "Used by"))
                for mixin in cls_refs.classrefs:
                    items.append(self.bullet(f"{self.class_link(mixin, use_desc=True)}"))

        if has_references_to:
            items.append(self.header(4, "Referenced by:"))
            self.list_classes(class_refs, cls, items)

        return items

    def slots_table(self, cls):
        slot_list = [slot for slot in [self.schema.slots[sn] for sn in cls.slots]]
        own_slots = {slot.name for slot in slot_list if cls.name in slot.domain_of}
        attributes = []

        for slot in slot_list:
            attributes.append({
                'Name': f'*{slot.name}*' if slot.name not in own_slots else slot.name,
                'Cardinality:': self.predicate_cardinality(slot),
                'Type': self.class_type_link(slot.range),
                'Description': slot.description if slot.description else ''
            })
        if len(attributes) > 0:
            return markdown_table(attributes).set_params(
                quote=False, row_sep='markdown').get_markdown()

    def get_class_children(self, class_name) -> Set[ClassDefinitionName]:
        if class_name in self.synopsis.classrefs:
            isa_refs = self.synopsis.isarefs.get(class_name)
            if isa_refs and isa_refs.classrefs:
                return isa_refs.classrefs
        return set({})

    def list_children(self, class_refs, items):
        for cls in sorted(class_refs):
            items.append(self.bullet(f"{self.class_link(cls, use_desc=True)}"))

    def list_classes(self, class_refs, cls, items):
        for sn in sorted(class_refs.slotrefs):
            slot = self.schema.slots[sn]
            if slot.range == cls.name:
                for dom in slot.domain_of:
                    items.append(
                        self.bullet(
                            f" **{self.class_link(dom)}** "
                            f"*{self.slot_link(slot, add_subset=False)}*{self.predicate_cardinality(slot)} "
                        )
                    )

    def visit_subset(self, subset: SubsetDefinition) -> str:
        if None:
            with open(self.exist_warning(self.dir_path(subset)), "w", encoding="UTF-8") as subsetfile:
                items = []
                curie = self.namespaces.uri_or_curie_for(str(self.namespaces._base), underscore(subset.name))
                uri = self.namespaces.uri_for(curie)
                items.append(self.element_header(obj=subset, name=subset.name, curie=curie, uri=uri))
                # TODO: consider showing hierarchy within a subset
                items.append(self.header(3, "Classes"))
                for cls in sorted(self.schema.classes.values(), key=lambda c: c.name):
                    if not cls.mixin:
                        if cls.in_subset and subset.name in cls.in_subset:
                            items.append(self.bullet(self.class_link(cls, use_desc=True), 0))
                items.append(self.header(3, "Mixins"))
                for cls in sorted(self.schema.classes.values(), key=lambda c: c.name):
                    if cls.mixin:
                        if cls.in_subset and subset.name in cls.in_subset:
                            items.append(self.bullet(self.class_link(cls, use_desc=True), 0))
                items.append(self.header(3, "Slots"))
                for slot in sorted(self.schema.slots.values(), key=lambda s: s.name):
                    if slot.in_subset and subset.name in slot.in_subset:
                        items.append(self.bullet(self.slot_link(slot, use_desc=True), 0))
                items.append(self.header(3, "Types"))
                for type in sorted(self.schema.types.values(), key=lambda s: s.name):
                    if type.in_subset and subset.name in type.in_subset:
                        items.append(self.bullet(self.type_link(type, use_desc=True), 0))
                items.append(self.header(3, "Enums"))
                for enum in sorted(self.schema.enums.values(), key=lambda s: s.name):
                    if enum.in_subset and subset.name in enum.in_subset:
                        items.append(self.bullet(self.enum_link(enum, use_desc=True), 0))
                items.append(self.element_properties(subset))
                out = "\n".join(items)
                out = pad_heading(out)
                subsetfile.write(out)
                return out

    def element_header(self, obj: Element, name: str, curie: str, uri: str) -> str:
        if isinstance(obj, TypeDefinition):
            obj_type = "Type"
        elif isinstance(obj, ClassDefinition):
            obj_type = "Class"
        elif isinstance(obj, SlotDefinition):
            obj_type = "Slot"
        elif isinstance(obj, EnumDefinition):
            obj_type = "Enum"
        elif isinstance(obj, SubsetDefinition):
            obj_type = "Subset"
        else:
            obj_type = "Class"

        header_label = f"~~{name}~~ _(deprecated)_" if obj.deprecated else f"{name}"
        out = self.header(3, header_label)

        out += self.para(be(obj.description))
        out = "\n".join([out, f"URI: [{curie}]({uri})", ""])
        return out

    def class_hier(self, cls: ClassDefinition, level=0) -> str:
        items = []
        items.append(self.bullet(self.class_link(cls, use_desc=True), level))
        if cls.name in sorted(self.synopsis.isarefs):
            for child in sorted(self.synopsis.isarefs[cls.name].classrefs):
                items.append(self.class_hier(self.schema.classes[child], level + 1))
        return "\n".join(items) if items else None

    def is_secondary_ref(self, en: str) -> bool:
        """Determine whether 'en' is the name of something in the neighborhood of the requested classes

        @param en: element name
        @return: True if 'en' is the name of a slot, class or type in the immediate neighborhood of of what we are
        building
        """
        if not self.gen_classes:
            return True
        elif en in self.schema.classes:
            return en in self.gen_classes_neighborhood.classrefs
        elif en in self.schema.slots:
            return en in self.gen_classes_neighborhood.slotrefs
        elif en in self.schema.types:
            return en in self.gen_classes_neighborhood.typerefs
        else:
            return True

    # --
    # FORMATTING
    # --
    @staticmethod
    def predicate_cardinality(slot: SlotDefinition) -> str:
        """Emit cardinality for a suffix on a predicate"""
        if slot.multivalued:
            card_str = "1..\\*" if slot.required else "0..\\*"
        else:
            card_str = "1..1" if slot.required else "0..1"
        return f"  <sub>{card_str}</sub>"

    @staticmethod
    def range_cardinality(slot: SlotDefinition) -> str:
        """Emits cardinality decorator at end of type"""
        if slot.multivalued:
            card_str = "1..\\*" if slot.required else "0..\\*"
        else:
            card_str = "1..1" if slot.required else "0..1"
        return f"  <sub><b>{card_str}</b></sub>"

    @staticmethod
    def anchor(id_: str) -> str:
        return f'<a name="{id_}">'

    @staticmethod
    def anchorend() -> str:
        return "</a>"

    def header(self, level: int, txt: str) -> str:
        txt = self.get_metamodel_slot_name(txt)
        out = f'\n{"#" * level} {txt}\n'
        return out

    @staticmethod
    def para(txt: str) -> str:
        return f"\n{txt}\n"

    @staticmethod
    def bullet(txt: str, level=0) -> str:
        return f'{"    " * level} * {txt}'

    def frontmatter(self, thingtype: str, layout="default") -> str:
        return self.header(1, thingtype)
        # print(f'---\nlayout: {layout}\n---\n')

    def bbin(self, obj: Element) -> str:
        """Boldify built in types

        @param obj: object name or id
        @return:
        """
        return obj.name if isinstance(obj, Element) else f"**{obj}**" if obj in self.synopsis.typebases else obj

    def desc_for(self, obj: Element, doing_descs: bool) -> str:
        """Return a description for object if it is unique (different than its parent)

        @param obj: object to be described
        @param doing_descs: If false, always return an empty string
        @return: text or empty string
        """
        if obj.description and doing_descs:
            if isinstance(obj, SlotDefinition) and obj.is_a:
                parent = self.schema.slots[obj.is_a]
            elif isinstance(obj, ClassDefinition) and obj.is_a:
                parent = self.schema.classes[obj.is_a]
            else:
                parent = None
            return "" if parent and obj.description == parent.description else obj.description
        return ""

    def _link(
            self,
            obj: Optional[Element],
            *,
            after_link: str = None,
            use_desc: bool = False,
            add_subset: bool = True,
    ) -> str:
        """Create a link to ref if appropriate.

        @param ref: the name or value of a class, slot, type or the name of a built in type.
        @param after_link: Text to put between link and description
        @param use_desc: True means append a description after the link if available
        @param add_subset: True means add any subset information that is available
        @return:
        """
        nl = "\n"
        if obj is None or not self.is_secondary_ref(obj.name):
            return self.bbin(obj)
        if isinstance(obj, TypeDefinition):
            link_name = camelcase(obj.name)
            link_ref = f"types/{link_name}" if not self.no_types_dir else f"{link_name}"
        elif isinstance(obj, ClassDefinition):
            link_name = camelcase(obj.name)
            link_ref = link_name.lower()
        elif isinstance(obj, EnumDefinition):
            link_name = camelcase(obj.name)
            link_ref = link_name.lower()
        elif isinstance(obj, SubsetDefinition):
            link_name = camelcase(obj.name)
            link_ref = camelcase(link_name)
        else:
            link_name = obj.name
            link_ref = link_name
        desc = self.desc_for(obj, use_desc)
        return f"[{link_name}]" f"(#{link_ref})" + (f" {after_link} " if after_link else "") + (
            f" - {desc.split(nl)[0]}" if desc else ""
        )

    def type_link(
            self,
            ref: Optional[Union[str, TypeDefinition]],
            *,
            after_link: str = None,
            use_desc: bool = False,
            add_subset: bool = True,
    ) -> str:
        return ref

    def slot_link(
            self,
            ref: Optional[Union[str, SlotDefinition]],
            *,
            after_link: str = None,
            use_desc: bool = False,
            add_subset: bool = True,
    ) -> str:
        return self._link(
            self.schema.slots[ref] if isinstance(ref, str) else ref,
            after_link=after_link,
            use_desc=use_desc,
            add_subset=add_subset,
        )

    def class_link(
            self,
            ref: Optional[Union[str, ClassDefinition]],
            *,
            after_link: str = None,
            use_desc: bool = False,
            add_subset: bool = True,
    ) -> str:
        return self._link(
            self.schema.classes[ref] if isinstance(ref, str) else ref,
            after_link=after_link,
            use_desc=use_desc,
            add_subset=add_subset,
        )

    def class_type_link(
            self,
            ref: Optional[Union[str, ClassDefinition, TypeDefinition, EnumDefinition]],
            *,
            after_link: str = None,
            use_desc: bool = False,
            add_subset: bool = True,
    ) -> str:
        if isinstance(ref, ClassDefinition):
            return self.class_link(ref, after_link=after_link, use_desc=use_desc, add_subset=add_subset)
        elif isinstance(ref, TypeDefinition):
            return self.type_link(ref, after_link=after_link, use_desc=use_desc, add_subset=add_subset)
        elif isinstance(ref, EnumDefinition):
            return self.type_link(ref, after_link=after_link, use_desc=use_desc, add_subset=add_subset)
        elif ref in self.schema.classes:
            return self.class_link(ref, after_link=after_link, use_desc=use_desc, add_subset=add_subset)
        elif ref in self.schema.enums:
            return self.enum_link(ref, after_link=after_link, use_desc=use_desc, add_subset=add_subset)
        else:
            return self.type_link(ref, after_link=after_link, use_desc=use_desc, add_subset=add_subset)

    def enum_link(
            self,
            ref: Optional[Union[str, EnumDefinition]],
            *,
            after_link: str = None,
            use_desc: bool = False,
            add_subset: bool = True,
    ) -> str:
        return self._link(
            self.schema.enums[ref] if isinstance(ref, str) else ref,
            after_link=after_link,
            use_desc=use_desc,
            add_subset=add_subset,
        )

    def subset_link(
            self,
            ref: Optional[Union[str, SubsetDefinition]],
            *,
            after_link: str = None,
            use_desc: bool = False,
    ) -> str:
        return self._link(
            self.schema.subsets[ref] if isinstance(ref, str) else ref,
            after_link=after_link,
            use_desc=use_desc,
        )


def pad_heading(text: str) -> str:
    """Add an extra newline to a non-top-level header that doesn't have one preceding it"""
    return re.sub(r"(?<!\n)\n##", "\n\n##", text)


@shared_arguments(MarkdownDataDictGen)
@click.command()
@click.option("--classes", "-c", multiple=True, help="Class(es) to emit")
@click.version_option(__version__, "-V", "--version")
def cli(yamlfile, **kwargs):
    """Generate markdown documentation of a LinkML model"""
    gen = MarkdownDataDictGen(yamlfile, **kwargs)
    print(gen.serialize(**kwargs))


if __name__ == "__main__":
    cli()
