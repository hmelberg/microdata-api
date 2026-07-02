from ._anvil_designer import AdminSourcesTemplate
from anvil import *
import anvil.server
import anvil.users


class AdminSources(AdminSourcesTemplate):
  """Admin CRUD for the `sources` Data Table. UI is built in code on a bare
  template; all reads/writes go through the admin_* server callables (never
  direct table access). Requires an is_admin user (Anvil Users login)."""

  def __init__(self, **properties):
    self.init_components(**properties)
    self._sources = []
    self._build_ui()
    user = anvil.users.get_user() or anvil.users.login_with_form()
    if user is None:
      self.status.text = "Ikke innlogget."
      return
    self._refresh()

  # ── UI ────────────────────────────────────────────────────────────────
  def _build_ui(self):
    p = self.content_panel
    p.add_component(Label(text="Datakilder (admin)", role="headline"))
    self.status = Label(text="", italic=True)

    self.picker = DropDown(include_placeholder=True, placeholder="— velg kilde —")
    self.picker.set_event_handler("change", self._on_pick)
    row_top = FlowPanel(spacing="small")
    row_top.add_component(self.picker)
    btn_new = Button(text="Ny", icon="fa:plus")
    btn_new.set_event_handler("click", self._on_new)
    row_top.add_component(btn_new)
    btn_refresh = Button(text="Oppdater liste", icon="fa:refresh")
    btn_refresh.set_event_handler("click", lambda **e: self._refresh())
    row_top.add_component(btn_refresh)
    p.add_component(row_top)

    self.f_source_id = TextBox(placeholder="source_id (bokstaver/tall/_/-)")
    self.f_name = TextBox(placeholder="visningsnavn")
    self.f_description = TextArea(placeholder="beskrivelse", height=60)
    self.f_kind = DropDown(items=["media", "url"], selected_value="media")
    self.f_location = TextBox(placeholder="https://… (kun for kind=url)")
    self.f_format = DropDown(items=["csv", "parquet"], selected_value="csv")
    self.f_level = DropDown(items=["public", "protected", "sensitive"],
                            selected_value="protected")
    self.f_exec = DropDown(items=["local", "remote", "strict_remote"],
                           selected_value="remote")
    self.f_file = FileLoader(text="Velg fil (csv/parquet)", multiple=False)

    grid = ColumnPanel()
    for lbl, comp in [("source_id", self.f_source_id), ("navn", self.f_name),
                      ("beskrivelse", self.f_description), ("kind", self.f_kind),
                      ("location", self.f_location), ("format", self.f_format),
                      ("nivå", self.f_level), ("default_exec", self.f_exec),
                      ("fil", self.f_file)]:
      row = FlowPanel(spacing="small")
      row.add_component(Label(text=lbl, width=110, bold=True))
      row.add_component(comp)
      grid.add_component(row)
    p.add_component(grid)

    row_btns = FlowPanel(spacing="small")
    btn_save = Button(text="Lagre", role="primary-color", icon="fa:save")
    btn_save.set_event_handler("click", self._on_save)
    row_btns.add_component(btn_save)
    self.btn_delete = Button(text="Slett (soft)", icon="fa:trash", enabled=False)
    self.btn_delete.set_event_handler("click", self._on_delete)
    row_btns.add_component(self.btn_delete)
    self.btn_restore = Button(text="Gjenopprett", icon="fa:undo", enabled=False)
    self.btn_restore.set_event_handler("click", self._on_restore)
    row_btns.add_component(self.btn_restore)
    p.add_component(row_btns)
    p.add_component(self.status)

    self.listing = DataGrid(columns=[
      {"id": "source_id", "title": "source_id", "data_key": "source_id"},
      {"id": "kind", "title": "kind", "data_key": "kind"},
      {"id": "level", "title": "nivå", "data_key": "level"},
      {"id": "status", "title": "status", "data_key": "status"},
      {"id": "encrypted", "title": "kryptert", "data_key": "encrypted"},
      {"id": "owner_email", "title": "eier", "data_key": "owner_email"},
    ], rows_per_page=15)
    p.add_component(self.listing)

  # ── data ──────────────────────────────────────────────────────────────
  def _refresh(self):
    try:
      self._sources = anvil.server.call("admin_list_sources")
    except Exception as e:
      self.status.text = f"Feil: {e}"
      return
    self.picker.items = [(f"{s['source_id']} ({s['status']})", s["source_id"])
                         for s in self._sources]
    # DataGrid: clear our previously added rows, then repopulate
    for c in list(self.listing.get_components()):
      if isinstance(c, DataRowPanel):
        c.remove_from_parent()
    for s in self._sources:
      r = DataRowPanel(item=s)
      self.listing.add_component(r)
    self.status.text = f"{len(self._sources)} kilder."

  def _on_pick(self, **event_args):
    sid = self.picker.selected_value
    s = next((x for x in self._sources if x["source_id"] == sid), None)
    if not s:
      return
    self.f_source_id.text = s["source_id"]
    self.f_name.text = s["name"]
    self.f_description.text = s["description"]
    self.f_kind.selected_value = s["kind"]
    self.f_location.text = s["location"]
    self.f_format.selected_value = s["format"]
    self.f_level.selected_value = s["level"]
    self.f_exec.selected_value = s["default_exec"] or "remote"
    self.f_file.clear()
    self.btn_delete.enabled = s["status"] == "active"
    self.btn_restore.enabled = s["status"] != "active"

  def _on_new(self, **event_args):
    self.picker.selected_value = None
    for tb in (self.f_source_id, self.f_name, self.f_location):
      tb.text = ""
    self.f_description.text = ""
    self.f_kind.selected_value = "media"
    self.f_format.selected_value = "csv"
    self.f_level.selected_value = "protected"
    self.f_exec.selected_value = "remote"
    self.f_file.clear()
    self.btn_delete.enabled = False
    self.btn_restore.enabled = False

  def _fields(self):
    return {
      "source_id": self.f_source_id.text,
      "name": self.f_name.text,
      "description": self.f_description.text,
      "kind": self.f_kind.selected_value,
      "location": self.f_location.text,
      "format": self.f_format.selected_value,
      "level": self.f_level.selected_value,
      "default_exec": self.f_exec.selected_value,
    }

  def _on_save(self, **event_args):
    self.status.text = "Lagrer…"
    try:
      saved = anvil.server.call("admin_save_source", self._fields(),
                                self.f_file.file)
      n = saved.get("nrows")
      self.status.text = ("Lagret " + saved["source_id"]
                          + (f" ({n} rader lest, validert)" if n else "")
                          + (" — kryptert på disk" if saved.get("encrypted") else ""))
      self._refresh()
    except Exception as e:
      self.status.text = f"Feil ved lagring: {e}"

  def _on_delete(self, **event_args):
    sid = self.f_source_id.text
    if not sid or not confirm(f"Soft-slette «{sid}»?"):
      return
    try:
      anvil.server.call("admin_delete_source", sid)
      self.status.text = f"Slettet {sid} (soft)."
      self._refresh()
    except Exception as e:
      self.status.text = f"Feil ved sletting: {e}"

  def _on_restore(self, **event_args):
    sid = self.f_source_id.text
    try:
      anvil.server.call("admin_restore_source", sid)
      self.status.text = f"Gjenopprettet {sid}."
      self._refresh()
    except Exception as e:
      self.status.text = f"Feil: {e}"
