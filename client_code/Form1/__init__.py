from ._anvil_designer import Form1Template
from anvil import *
import anvil.users
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables

class Form1(Form1Template):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)

    # Admin entry: the AdminSources form does its own login + is_admin check.
    admin_link = Link(text="Admin: datakilder", icon="fa:database")
    admin_link.set_event_handler("click", lambda **e: open_form("AdminSources"))
    self.navbar_links.add_component(admin_link)
