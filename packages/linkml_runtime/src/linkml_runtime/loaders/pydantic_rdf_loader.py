"""
RDF loader for Pydantic models that uses embedded LinkML metadata.

This loader works with RDF data and converts it back to Pydantic models
using only the embedded linkml_meta without requiring a SchemaView.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO, Union

# Sentinel returned by _convert_object_value when a URI cannot be resolved to
# a model instance and no lazy factory is available.  The caller skips adding
# this value to model_data, preventing ValidationError on unresolved refs.
_SKIP: object = object()

from hbreader import FileInfo
from pydantic import BaseModel
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF

from linkml_runtime.loaders.loader_root import Loader
from linkml_runtime.utils.yamlutils import YAMLRoot

logger = logging.getLogger(__name__)


class PydanticRDFLoader(Loader):
    """
    Loads RDF data into Pydantic models using embedded LinkML metadata.

    This loader uses the linkml_meta attributes embedded in Pydantic models
    to understand the RDF structure without requiring a separate SchemaView.

    Enhanced with generic capabilities for real-world RDF data:
    - Multilingual string handling with language preferences
    - Smart cardinality handling for external ontology mismatches
    - Complex object instantiation from URI references
    """

    def __init__(
        self,
        preferred_languages: list[str] | None = None,
        *,
        identity_map: Any = None,
        lazy_loader_factory: Callable[[str, type[BaseModel]], BaseModel] | None = None,
    ):
        """
        Initialize PydanticRDFLoader with enhanced capabilities.

        Args:
            preferred_languages: Language preference order (e.g., ['en', 'ja', 'de'])
            identity_map: Optional identity map for object deduplication and cycle
                detection. Must support ``.get(type, uri)`` and ``.put(type, uri, instance)``.
                If not provided, an internal dict is used for per-load cycle detection.
            lazy_loader_factory: Optional factory ``(full_uri, target_type) -> BaseModel``
                called when a URIRef has no ``rdf:type`` in the current graph but the
                field expects a complex type.  The factory returns a lazy proxy that
                loads on first field access.  When ``None``, such URIs are silently
                skipped (field omitted from model_data).
        """
        self.preferred_languages = preferred_languages or ["en"]
        self._identity_map = identity_map
        self._lazy_loader_factory = lazy_loader_factory

    def load(
        self,
        source: Union[str, Graph],
        target_class: type[BaseModel],
        fmt: str = "turtle",
        prefix_map: dict[str, str] | None = None,
        *,
        root_subject: str | None = None,
    ) -> BaseModel:
        """
        Load RDF data into a Pydantic model instance

        :param source: RDF data (file path, string, or Graph)
        :param target_class: Target Pydantic model class
        :param fmt: RDF format if source is string/file
        :param prefix_map: Optional prefix mappings
        :param root_subject: Optional URI string of the root subject to load.
            When provided, ``_find_root_subject`` is skipped and this URI is used
            directly.  Required when the graph contains multiple subjects with the
            same ``rdf:type`` as ``target_class`` (e.g. depth-1 graph assembly).
        :return: Pydantic model instance
        """
        # Parse RDF if needed
        if isinstance(source, Graph):
            graph = source
        else:
            graph = Graph()
            if isinstance(source, str):
                # Check if it's RDF content or a file path
                source_stripped = source.strip()
                if (
                    source_stripped.startswith(("@prefix", "PREFIX", "<", "_:", "{", "["))
                    or "\n" in source_stripped
                    or len(source_stripped) > 200
                ):
                    # It's RDF content
                    graph.parse(data=source, format=fmt)
                else:
                    # It's a file path
                    graph.parse(source, format=fmt)
            else:
                # Handle Path, TextIO, etc.
                graph.parse(source, format=fmt)

        # Add optional prefix mappings
        if prefix_map:
            for prefix, uri in prefix_map.items():
                graph.namespace_manager.bind(prefix, URIRef(uri))

        # Extract global metadata
        global_meta = self._extract_global_metadata(target_class)

        # Build prefix mappings from metadata
        prefixes = {}
        if "prefixes" in global_meta:
            for prefix_info in global_meta["prefixes"].values():
                prefixes[prefix_info["prefix_prefix"]] = prefix_info["prefix_reference"]

        # Resolve the root subject: use caller-provided URI when available so
        # that multi-subject graphs (depth-1 assembly) always start from the
        # correct node rather than a non-deterministic first match.
        if root_subject is not None:
            root_subject_ref: URIRef = URIRef(root_subject)
        else:
            root_subject_ref = self._find_root_subject(graph, target_class, global_meta, prefixes)

        # Per-load dict acts as a secondary cache when no external identity_map is
        # provided.  Two-phase shell pre-registration (in _convert_subject_to_model)
        # handles circular references without a separate processing/visited set.
        load_cache: dict[tuple[str, str], BaseModel] = {}

        # Convert RDF to Pydantic model
        return self._convert_subject_to_model(graph, root_subject_ref, target_class, global_meta, prefixes, load_cache)

    def _extract_global_metadata(self, model_class: type[BaseModel]) -> dict[str, Any]:
        """Extract global metadata from the model class module"""
        module = model_class.__module__
        try:
            import importlib

            module_obj = importlib.import_module(module)
            if hasattr(module_obj, "linkml_meta"):
                return module_obj.linkml_meta.root
        except ImportError:
            pass
        return {}

    def _find_root_subject(
        self, graph: Graph, target_class: type[BaseModel], global_meta: dict[str, Any], prefixes: dict[str, str]
    ) -> URIRef:
        """Find the root subject in the graph for the target class.

        Looks for a subject whose rdf:type matches the class URI from metadata.
        When the model has no explicit class_uri, constructs a fallback from
        ``from_schema#ClassName`` (matching the convention used by LinkML
        codegen and schema_utils.get_class_uri).
        """
        class_meta = self._get_class_metadata(target_class)
        class_uri = class_meta.get("class_uri")

        # Build candidate type URIs to search for
        candidate_uris: list[str] = []
        if class_uri:
            candidate_uris.append(self._expand_curie(class_uri, prefixes))

        # Fallback: from_schema#ClassName (same logic as get_class_uri)
        from_schema = class_meta.get("from_schema", global_meta.get("default_range", ""))
        if from_schema:
            candidate_uris.append(f"{from_schema}#{target_class.__name__}")

        for uri in candidate_uris:
            for subject in graph.subjects(RDF.type, URIRef(uri)):
                return subject

        # Last resort: first non-blank subject
        for subject in graph.subjects():
            if not isinstance(subject, BNode):
                return subject

        for subject in graph.subjects():
            return subject

        raise ValueError("No suitable subject found in RDF graph")

    def _get_class_metadata(self, model_class: type[BaseModel]) -> dict[str, Any]:
        """Extract class metadata from Pydantic model class"""
        if hasattr(model_class, "linkml_meta"):
            return model_class.linkml_meta.root
        return {"name": model_class.__name__}

    def _expand_curie(self, curie_or_uri: str, prefixes: dict[str, str]) -> str:
        """Expand a CURIE to full URI"""
        if "://" in curie_or_uri:
            return curie_or_uri
        if ":" in curie_or_uri:
            prefix, local = curie_or_uri.split(":", 1)
            if prefix in prefixes:
                return prefixes[prefix] + local
        return curie_or_uri

    def _convert_subject_to_model(
        self,
        graph: Graph,
        subject: URIRef,
        target_class: type[BaseModel],
        global_meta: dict[str, Any],
        prefixes: dict[str, str],
        load_cache: dict[tuple[str, str], BaseModel] | None = None,
    ) -> BaseModel:
        """Convert an RDF subject to a Pydantic model instance.

        Two-phase construction: a shell is pre-registered in the cache before
        predicates are processed so that circular references (A→B→A) find the
        shell rather than recursing infinitely.  After the real instance is built
        its fields are copied in-place to the shell, preserving object identity.
        """
        subject_str = str(subject) if not isinstance(subject, BNode) else None

        # Check cache — may return a pre-registered shell (circular ref) or a
        # fully-built instance from an earlier load.
        if subject_str:
            cached = self._lookup_cache(target_class, subject_str, load_cache)
            if cached is not None:
                return cached

        # Phase 1: pre-register a shell so recursive calls find it in cache.
        shell: BaseModel | None = None
        if subject_str:
            id_curie = self._uri_to_curie(subject_str, prefixes)
            shell = target_class.model_construct(id=id_curie)
            self._store_cache(target_class, subject_str, shell, load_cache)

        # Build model_data from RDF predicates.
        model_data: dict[str, Any] = {}
        if subject_str:
            model_data["id"] = self._uri_to_curie(subject_str, prefixes)

        for predicate, obj in graph.predicate_objects(subject):
            if predicate == RDF.type:
                continue

            field_name = self._find_field_for_predicate(target_class, predicate, prefixes)
            if not field_name:
                continue

            field_value = self._convert_object_value(
                graph, obj, target_class, field_name, global_meta, prefixes, load_cache
            )

            if field_value is _SKIP:
                continue

            if field_name in model_data:
                if not isinstance(model_data[field_name], list):
                    model_data[field_name] = [model_data[field_name]]
                model_data[field_name].append(field_value)
            else:
                model_data[field_name] = field_value

        self._handle_multilingual_strings(model_data, target_class)
        self._wrap_list_fields(model_data, target_class)

        # Phase 2: build validated instance.
        instance = target_class(**model_data)

        # Copy fields from the validated instance into the shell in-place so
        # any references captured during Phase 1 processing see full data.
        if shell is not None:
            for key, value in instance.__dict__.items():
                object.__setattr__(shell, key, value)
            if hasattr(instance, "__pydantic_fields_set__"):
                object.__setattr__(shell, "__pydantic_fields_set__", instance.__pydantic_fields_set__)
            self._store_cache(target_class, subject_str, shell, load_cache)
            return shell

        return instance

    def _wrap_list_fields(self, model_data: dict[str, Any], target_class: type[BaseModel]) -> None:
        """Wrap single values in lists for fields that expect lists"""
        import typing

        for field_name, field_info in target_class.model_fields.items():
            if field_name not in model_data:
                continue

            # Check if field is already a list
            if isinstance(model_data[field_name], list):
                continue

            # Check if the field type is a list type
            field_type = field_info.annotation

            # Handle Optional[list[...]] and similar Union types
            if hasattr(typing, "get_origin") and hasattr(typing, "get_args"):
                origin = typing.get_origin(field_type)
                args = typing.get_args(field_type)

                # Handle Union types (like Optional[list[str]])
                if origin is typing.Union:
                    for arg in args:
                        if typing.get_origin(arg) is list:
                            # This field should be a list, wrap the single value
                            model_data[field_name] = [model_data[field_name]]
                            break
                # Handle direct list types
                elif origin is list:
                    model_data[field_name] = [model_data[field_name]]

    def _uri_to_curie(self, uri: str, prefixes: dict[str, str]) -> str:
        """Convert URI back to CURIE if possible"""
        for prefix, base_uri in prefixes.items():
            if uri.startswith(base_uri):
                return f"{prefix}:{uri[len(base_uri) :]}"
        return uri

    def _lookup_cache(
        self,
        target_class: type[BaseModel],
        uri: str,
        load_cache: dict[tuple[str, str], BaseModel] | None = None,
    ) -> BaseModel | None:
        """Check external identity map first, then per-load cache."""
        if self._identity_map is not None:
            hit = self._identity_map.get(target_class, uri)
            if hit is not None:
                return hit
        if load_cache is not None:
            key = (target_class.__name__, uri)
            if key in load_cache:
                return load_cache[key]
        return None

    def _store_cache(
        self,
        target_class: type[BaseModel],
        uri: str,
        instance: BaseModel,
        load_cache: dict[tuple[str, str], BaseModel] | None = None,
    ) -> None:
        """Store in external identity map and per-load cache."""
        if self._identity_map is not None:
            self._identity_map.put(target_class, uri, instance)
        if load_cache is not None:
            load_cache[(target_class.__name__, uri)] = instance

    def _find_field_for_predicate(
        self, model_class: type[BaseModel], predicate: URIRef, prefixes: dict[str, str]
    ) -> str | None:
        """Find the model field that corresponds to an RDF predicate"""
        predicate_str = str(predicate)

        # Check each field's metadata
        for field_name, field_info in model_class.model_fields.items():
            field_meta = self._get_field_metadata(field_info)

            # Check slot_uri
            slot_uri = field_meta.get("slot_uri")
            if slot_uri:
                expanded_slot_uri = self._expand_curie(slot_uri, prefixes)
                if expanded_slot_uri == predicate_str:
                    return field_name

            # Check exact_mappings
            exact_mappings = field_meta.get("exact_mappings", [])
            for mapping in exact_mappings:
                expanded_mapping = self._expand_curie(mapping, prefixes)
                if expanded_mapping == predicate_str:
                    return field_name

            # Check constructed URI from from_schema + field_name
            from_schema = field_meta.get("from_schema", "")
            if from_schema:
                if not from_schema.endswith(("/", "#")):
                    from_schema += "/"
                constructed_uri = from_schema + field_name
                if constructed_uri == predicate_str:
                    return field_name

        return None

    def _get_field_metadata(self, field_info) -> dict[str, Any]:
        """Extract metadata from Pydantic field"""
        if hasattr(field_info, "json_schema_extra") and field_info.json_schema_extra:
            linkml_meta = field_info.json_schema_extra.get("linkml_meta", {})
            return linkml_meta
        return {}

    def _convert_object_value(
        self,
        graph: Graph,
        obj: Union[URIRef, Literal, BNode],
        model_class: type[BaseModel],
        field_name: str,
        global_meta: dict[str, Any],
        prefixes: dict[str, str],
        load_cache: dict[tuple[str, str], BaseModel] | None = None,
    ) -> Any:
        """Convert an RDF object to appropriate Python value.

        Returns the module-level ``_SKIP`` sentinel when a URIRef resolves to a
        complex field type but its triples are absent from the current graph and
        no ``lazy_loader_factory`` is configured.  Callers must check for this
        sentinel and omit the field from model_data.
        """
        if isinstance(obj, Literal):
            if obj.datatype:
                if obj.datatype == URIRef("http://www.w3.org/2001/XMLSchema#integer"):
                    return int(obj)
                elif obj.datatype == URIRef("http://www.w3.org/2001/XMLSchema#decimal"):
                    return float(obj)
                elif obj.datatype == URIRef("http://www.w3.org/2001/XMLSchema#boolean"):
                    return str(obj).lower() in ("true", "1")
                elif obj.datatype == URIRef("http://www.w3.org/2001/XMLSchema#date"):
                    from datetime import date

                    return date.fromisoformat(str(obj))

            if obj.language:
                return (str(obj), obj.language)
            else:
                return str(obj)

        elif isinstance(obj, URIRef):
            obj_str = str(obj)
            obj_types = list(graph.objects(obj, RDF.type))

            if obj_types:
                # URIRef has rdf:type in the graph — convert to a model instance.
                target_field_class = self._get_field_target_class(model_class, field_name, global_meta)
                if target_field_class:
                    cached = self._lookup_cache(target_field_class, obj_str, load_cache)
                    if cached is not None:
                        return cached
                    # Circular refs are broken by the pre-registered shell in
                    # _convert_subject_to_model; no explicit processing set needed.
                    return self._convert_subject_to_model(
                        graph, obj, target_field_class, global_meta, prefixes, load_cache
                    )
            else:
                # No rdf:type in graph for this URI.  If the field expects a
                # complex type, return a lazy proxy (when factory is available)
                # or _SKIP (so the caller omits the field rather than creating a
                # ValidationError by returning a raw CURIE string).
                target_field_class = self._get_field_target_class(model_class, field_name, global_meta)
                if target_field_class:
                    cached = self._lookup_cache(target_field_class, obj_str, load_cache)
                    if cached is not None:
                        return cached
                    if self._lazy_loader_factory is not None:
                        proxy = self._lazy_loader_factory(obj_str, target_field_class)
                        self._store_cache(target_field_class, obj_str, proxy, load_cache)
                        return proxy
                    return _SKIP

            return self._uri_to_curie(obj_str, prefixes)

        elif isinstance(obj, BNode):
            target_field_class = self._get_field_target_class(model_class, field_name, global_meta)
            if target_field_class:
                return self._convert_subject_to_model(graph, obj, target_field_class, global_meta, prefixes, load_cache)
            return str(obj)

        return str(obj)

    def _get_field_target_class(
        self, model_class: type[BaseModel], field_name: str, global_meta: dict[str, Any]
    ) -> type[BaseModel] | None:
        """Get the target class for a field if it's a complex object.

        Uses the Python type annotation as the source of truth.  When the
        annotation resolves to a primitive type (``str``, ``int``, …) we
        return ``None`` immediately — the metadata ``range`` is not used as a
        fallback in that case because the code generator deliberately chose a
        primitive type (the field is not inlined).
        """
        import typing
        from datetime import date, datetime, time
        from decimal import Decimal

        field_info = model_class.model_fields.get(field_name)
        if not field_info:
            return None

        # First try to get the class from type annotation
        field_type = field_info.annotation

        # Handle Optional and List types
        if hasattr(typing, "get_origin") and hasattr(typing, "get_args"):
            origin = typing.get_origin(field_type)
            args = typing.get_args(field_type)

            # Handle Union types (like Optional[Type])
            if origin is typing.Union:
                for arg in args:
                    # Skip None type
                    if arg is type(None):
                        continue
                    # Check if it's a list
                    if typing.get_origin(arg) is list:
                        inner_args = typing.get_args(arg)
                        if inner_args and isinstance(inner_args[0], type) and issubclass(inner_args[0], BaseModel):
                            return inner_args[0]
                    # Check if it's a direct BaseModel subclass
                    elif isinstance(arg, type) and issubclass(arg, BaseModel):
                        return arg
            # Handle direct list types
            elif origin is list:
                if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
                    return args[0]

        # Check if it's a direct BaseModel subclass (no Optional/List wrapper)
        if isinstance(field_type, type) and issubclass(field_type, BaseModel):
            return field_type

        # If the annotation resolved to a known primitive type, trust it and
        # do NOT fall through to the metadata range lookup.  The code
        # generator uses str for non-inlined references intentionally.
        _primitive_types = {str, int, float, bool, bytes, date, datetime, time, Decimal}
        unwrapped = field_type
        if typing.get_origin(unwrapped) is typing.Union:
            non_none = [a for a in typing.get_args(unwrapped) if a is not type(None)]
            if len(non_none) == 1:
                unwrapped = non_none[0]
        if typing.get_origin(unwrapped) is list:
            inner = typing.get_args(unwrapped)
            if inner:
                unwrapped = inner[0]
        if unwrapped in _primitive_types:
            return None

        # Fallback to metadata approach
        field_meta = self._get_field_metadata(field_info)
        range_name = field_meta.get("range")

        if not range_name:
            return None

        # Try to find the class in the same module
        module = model_class.__module__
        try:
            import importlib

            module_obj = importlib.import_module(module)
            if hasattr(module_obj, range_name):
                target_class = getattr(module_obj, range_name)
                if isinstance(target_class, type) and issubclass(target_class, BaseModel):
                    return target_class
        except (ImportError, AttributeError):
            pass

        return None

    def loads(
        self,
        source: str,
        target_class: type[BaseModel],
        fmt: str = "turtle",
        prefix_map: dict[str, str] | None = None,
        *,
        root_subject: str | None = None,
    ) -> BaseModel:
        """
        Load RDF string into a Pydantic model instance

        :param source: RDF string content
        :param target_class: Target Pydantic model class
        :param fmt: RDF format
        :param prefix_map: Optional prefix mappings
        :param root_subject: Optional URI string of the root subject to load.
            Forwarded to ``load()``.  Use when the graph contains multiple
            subjects with the same ``rdf:type`` as ``target_class``.
        :return: Pydantic model instance
        """
        return self.load(source, target_class, fmt=fmt, prefix_map=prefix_map, root_subject=root_subject)

    def load_any(
        self,
        source: Union[str, dict, TextIO, Path],
        target_class: type[Union[BaseModel, YAMLRoot]],
        *,
        base_dir: str | None = None,
        metadata: FileInfo | None = None,
        fmt: str = "turtle",
        prefix_map: dict[str, str] | None = None,
        **_,
    ) -> Union[BaseModel, YAMLRoot, list[BaseModel], list[YAMLRoot]]:
        """
        Load source as an instance of target_class

        :param source: Source file/text/url to load
        :param target_class: Target class
        :param base_dir: Base directory for relative paths
        :param metadata: File metadata
        :param fmt: RDF format
        :param prefix_map: Optional prefix mappings
        :return: Instance of target_class
        """
        # For now, only support Pydantic BaseModel classes
        if not issubclass(target_class, BaseModel):
            raise ValueError("PydanticRDFLoader only supports Pydantic BaseModel classes")

        return self.load(source, target_class, fmt=fmt, prefix_map=prefix_map)

    def _handle_multilingual_strings(self, model_data: dict[str, Any], target_class: type[BaseModel]) -> None:
        """Handle multilingual string fields by selecting preferred language.

        Applies to both scalar string fields (``str``, ``Optional[str]``) and
        list-of-string fields (``list[str]``, ``Optional[list[str]]``).
        Language-tagged tuples ``(text, lang)`` are resolved via
        ``preferred_languages``.
        """
        import typing

        for field_name, field_info in target_class.model_fields.items():
            if field_name not in model_data:
                continue

            value = model_data[field_name]
            field_type = field_info.annotation

            origin = typing.get_origin(field_type)
            args = typing.get_args(field_type)

            # Detect the "inner" type, unwrapping Optional[...]
            inner_type = field_type
            if origin is typing.Union and args:
                non_none_args = [arg for arg in args if arg is not type(None)]
                if len(non_none_args) == 1:
                    inner_type = non_none_args[0]

            inner_origin = typing.get_origin(inner_type)
            inner_args = typing.get_args(inner_type)

            # Case 1: scalar string field (str / Optional[str])
            if inner_type is str:
                if isinstance(value, tuple) and len(value) == 2:
                    model_data[field_name] = value[0]
                elif isinstance(value, list) and len(value) > 1:
                    model_data[field_name] = self._select_preferred_language_from_tagged_literals(value)
                elif isinstance(value, list) and len(value) == 1 and isinstance(value[0], tuple):
                    model_data[field_name] = value[0][0]

            # Case 2: list-of-string field (list[str] / Optional[list[str]])
            elif inner_origin is list and inner_args and inner_args[0] is str:
                if isinstance(value, list) and value and isinstance(value[0], tuple):
                    # All items are language-tagged tuples — select preferred language
                    model_data[field_name] = [self._select_preferred_language_from_tagged_literals(value)]
                elif isinstance(value, list):
                    # Unwrap any stray tuples in the list
                    model_data[field_name] = [v[0] if isinstance(v, tuple) and len(v) == 2 else str(v) for v in value]

    def _select_preferred_language_from_tagged_literals(self, values: list[Any]) -> str:
        """
        Select preferred language from a list that may contain language-tagged literals.

        Real-world RDF data often mixes language-tagged literals (e.g. "Pflanze"@de)
        with untagged literals (e.g. "Grass").  Untagged literals are commonly the
        default/English value, so they should be preferred over non-matching tagged
        values.

        Args:
            values: List that may contain strings or (string, language) tuples

        Returns:
            Selected string value based on language preference
        """
        # Separate language-tagged and non-tagged values
        tagged_values: list[tuple[str, str]] = []
        non_tagged_values: list[str] = []

        for value in values:
            if isinstance(value, tuple) and len(value) == 2:
                tagged_values.append((value[0], value[1]))
            else:
                non_tagged_values.append(str(value))

        # 1. Try each preferred language in tagged values
        for preferred_lang in self.preferred_languages:
            for text, lang_code in tagged_values:
                if lang_code == preferred_lang:
                    return text

        # 2. Fall back to non-tagged values (often the default/English text)
        if non_tagged_values:
            return self._select_preferred_language(non_tagged_values)

        # 3. Last resort: first tagged value alphabetically by language
        if tagged_values:
            tagged_values.sort(key=lambda x: x[1])
            return tagged_values[0][0]

        return str(values[0]) if values else ""

    def _select_preferred_language(self, values: list[str]) -> str:
        """
        Select preferred language from a list of multilingual strings.

        Uses simple heuristics to identify English vs. other languages.
        Future enhancement: proper language detection based on RDF language tags.
        """
        if not values:
            return ""

        if len(values) == 1:
            return values[0]

        # Simple heuristic: prefer ASCII strings (likely English)
        for value in values:
            if all(ord(char) < 128 for char in value):
                return value

        # Fallback to first value
        return values[0]
