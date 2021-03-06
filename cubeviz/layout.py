import os
from collections import OrderedDict

from astropy.wcs.utils import wcs_to_celestial_frame
from astropy.coordinates import BaseRADecFrame

from qtpy import QtWidgets, QtCore
from qtpy.QtWidgets import QMenu, QAction, QInputDialog, QActionGroup

from glue.utils.qt import load_ui
from glue.utils.qt import get_qapp
from glue.config import qt_fixed_layout_tab
from glue.external.echo import keep_in_sync, SelectionCallbackProperty
from glue.external.echo.qt import connect_combo_selection
from glue.core.data_combo_helper import ComponentIDComboHelper
from glue.core.message import SettingsChangeMessage, SubsetUpdateMessage, SubsetDeleteMessage
from glue.utils.matplotlib import freeze_margins

from specviz.third_party.glue.data_viewer import SpecVizViewer
from specviz.core.events import dispatch

from .toolbar import CubevizToolbar
from .image_viewer import CubevizImageViewer

from .controls.slice import SliceController
from .controls.overlay import OverlayController
from .controls.units import UnitController
from .tools import arithmetic_gui, moment_maps, smoothing
from .tools import collapse_cube
from .tools.spectral_operations import SpectralOperationHandler


DEFAULT_NUM_SPLIT_VIEWERS = 3


class WidgetWrapper(QtWidgets.QWidget):

    def __init__(self, widget=None, tab_widget=None, parent=None):
        super(WidgetWrapper, self).__init__(parent=parent)
        self.tab_widget = tab_widget
        self._widget = widget
        self.layout = QtWidgets.QVBoxLayout()
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.addWidget(widget)
        self.setLayout(self.layout)

    def widget(self):
        return self._widget


@qt_fixed_layout_tab
class CubeVizLayout(QtWidgets.QWidget):
    """
    The 'CubeViz' layout, with three image viewers and one spectrum viewer.
    """

    LABEL = "CubeViz"
    subWindowActivated = QtCore.Signal(object)

    single_viewer_attribute = SelectionCallbackProperty(default_index=0)
    viewer1_attribute = SelectionCallbackProperty(default_index=0)
    viewer2_attribute = SelectionCallbackProperty(default_index=1)
    viewer3_attribute = SelectionCallbackProperty(default_index=2)

    def __init__(self, session=None, parent=None):
        super(CubeVizLayout, self).__init__(parent=parent)

        self._cubeviz_toolbar = None

        if not hasattr(session.application, '_has_cubeviz_toolbar'):
            self._cubeviz_toolbar = CubevizToolbar(application=session.application)
            session.application.insertToolBar(session.application._data_toolbar,
                                              self._cubeviz_toolbar)

        self.session = session
        self._has_data = False
        self._wavelengths = None
        self._option_buttons = []

        self._data = None

        self.ui = load_ui('layout.ui', self,
                          directory=os.path.dirname(__file__))

        self.cube_views = []

        # Create the cube viewers and register to the hub.
        for _ in range(DEFAULT_NUM_SPLIT_VIEWERS + 1):
            ww = WidgetWrapper(CubevizImageViewer(
                    self.session, cubeviz_layout=self), tab_widget=self)
            self.cube_views.append(ww)
            ww._widget.register_to_hub(self.session.hub)

        # Create specviz viewer and register to the hub.
        self.specviz = WidgetWrapper(SpecVizViewer(self.session), tab_widget=self)
        self.specviz._widget.register_to_hub(self.session.hub)

        self.single_view = self.cube_views[0]
        self.split_views = self.cube_views[1:]

        self._synced_checkboxes = [
            self.ui.singleviewer_synced_checkbox,
            self.ui.viewer1_synced_checkbox,
            self.ui.viewer2_synced_checkbox,
            self.ui.viewer3_synced_checkbox
        ]

        for view, checkbox in zip(self.cube_views, self._synced_checkboxes):
            view._widget.assign_synced_checkbox(checkbox)

        # Add the views to the layouts.
        self.ui.single_image_layout.addWidget(self.single_view)
        for viewer in self.split_views:
            self.ui.image_row_layout.addWidget(viewer)

        self.ui.specviz_layout.addWidget(self.specviz)

        self.subWindowActivated.connect(self._update_active_view)

        self.ui.sync_button.clicked.connect(self._on_sync_click)
        self.ui.button_toggle_image_mode.clicked.connect(
            self._toggle_image_mode)

        # This is a list of helpers for the viewer combo boxes. New data
        # collections should be added to each helper in this list using the
        # ``append_data`` method to ensure that the new data components are
        # populated into the combo boxes.
        self._viewer_combo_helpers = []

        # This tracks the current positions of cube viewer axes when they are hidden
        self._viewer_axes_positions = []

        # Indicates whether cube viewer toolbars are currently visible or not
        self._toolbars_visible = True

        # Indicates whether subset stats should be displayed or not
        self._stats_visible = True

        self._slice_controller = SliceController(self)
        self._overlay_controller = OverlayController(self)
        self._units_controller = UnitController(self)

        # Add menu buttons to the cubeviz toolbar.
        self.ra_dec_format_menu = None
        self._init_menu_buttons()

        self.sync = {}
        # Track the slice index of the synced viewers. This is updated by the
        # slice controller
        self.synced_index = None

        app = get_qapp()
        app.installEventFilter(self)
        self._last_click = None
        self._active_view = None
        self._active_cube = None
        self._last_active_view = None
        self._active_split_cube = None

        # Set the default to parallel image viewer
        self._single_viewer_mode = False
        self.ui.button_toggle_image_mode.setText('Single Image Viewer')
        self.ui.viewer_control_frame.setCurrentIndex(0)

        # Add this class to the specviz dispatcher watcher
        dispatch.setup(self)

    def _init_menu_buttons(self):
        """
        Add the two menu buttons to the tool bar. Currently two are defined:
            View - for changing the view of the active window
            Data Processing - for applying a data processing step to the data.

        :return:
        """
        self._option_buttons = [
            self.ui.view_option_button,
            self.ui.cube_option_button
        ]

        # Create the View Menu
        view_menu = self._dict_to_menu(OrderedDict([
            ('Hide Axes', ['checkable', self._toggle_viewer_axes]),
            ('Hide Toolbars', ['checkable', self._toggle_toolbars]),
            ('Hide Stats', ['checkable', self._toggle_stats_display]),
            ('Wavelength Units', lambda: self._open_dialog('Wavelength Units', None))
        ]))

        # Add toggle RA-DEC format:
        format_menu = view_menu.addMenu("RA-DEC Format")
        format_action_group = QActionGroup(format_menu)
        self.ra_dec_format_menu = format_menu

        # Make sure to change all instances of the the names
        # of the formats if modifications are made to them.
        for format_name in ["Sexagesimal", "Decimal Degrees"]:
            act = QAction(format_name, format_menu)
            act.triggered.connect(self._toggle_all_coords_in_degrees)
            act.setActionGroup(format_action_group)
            act.setCheckable(True)
            act.setChecked(True) if format == "Sexagesimal" else act.setChecked(False)
            format_menu.addAction(act)

        self.ui.view_option_button.setMenu(view_menu)

        # Create the Data Processing Menu
        cube_menu = self._dict_to_menu(OrderedDict([
            ('Collapse Cube', lambda: self._open_dialog('Collapse Cube', None)),
            ('Spatial Smoothing', lambda: self._open_dialog('Spatial Smoothing', None)),
            ('Moment Maps', lambda: self._open_dialog('Moment Maps', None)),
            ('Arithmetic Operations', lambda: self._open_dialog('Arithmetic Operations', None))
        ]))
        self.ui.cube_option_button.setMenu(cube_menu)

    def _dict_to_menu(self, menu_dict):
        '''Stolen shamelessly from specviz. Thanks!'''
        menu_widget = QMenu()
        for k, v in menu_dict.items():
            if isinstance(v, dict):
                new_menu = menu_widget.addMenu(k)
                self._dict_to_menu(v, menu_widget=new_menu)
            else:
                act = QAction(k, menu_widget)

                if isinstance(v, list):
                    if v[0] == 'checkable':
                        v = v[1]
                        act.setCheckable(True)
                        act.setChecked(False)

                act.triggered.connect(v)
                menu_widget.addAction(act)
        return menu_widget

    def handle_settings_change(self, message):
        if isinstance(message, SettingsChangeMessage):
            self._slice_controller.update_index(self.synced_index)

    def handle_subset_action(self, message):
        if isinstance(message, SubsetUpdateMessage):
            for combo, viewer in zip(self._viewer_combo_helpers, self.cube_views):
                viewer._widget.draw_stats_axes(combo.selection, message.subset)
        elif isinstance(message, SubsetDeleteMessage):
            for viewer in self.cube_views:
                viewer._widget.hide_stats_axes()

    def _set_pos_and_margin(self, axes, pos, marg):
        axes.set_position(pos)
        freeze_margins(axes, marg)

    def _hide_viewer_axes(self):
        for viewer in self.cube_views:
            viewer._widget.toggle_hidden_axes(True)
            axes = viewer._widget.axes
            # Save current axes position and margins so they can be restored
            pos = axes.get_position(), axes.resizer.margins
            self._viewer_axes_positions.append(pos)
            self._set_pos_and_margin(axes, [0, 0, 1, 1], [0, 0, 0, 0])
            viewer._widget.figure.canvas.draw()

    def _toggle_viewer_axes(self):
        # If axes are currently hidden, restore the original positions
        if self._viewer_axes_positions:
            for viewer, pos in zip(self.cube_views, self._viewer_axes_positions):
                viewer._widget.toggle_hidden_axes(False)
                axes = viewer._widget.axes
                self._set_pos_and_margin(axes, *pos)
                viewer._widget.figure.canvas.draw()
            self._viewer_axes_positions = []
        # Record current positions if axes are currently hidden and hide them
        else:
            self._hide_viewer_axes()

    def _toggle_toolbars(self):
        self._toolbars_visible = not self._toolbars_visible
        for viewer in self.cube_views:
            viewer._widget.toolbar.setVisible(self._toolbars_visible)

    def _toggle_stats_display(self):
        self._stats_visible = not self._stats_visible
        for viewer in self.cube_views:
            viewer._widget.set_stats_visible(self._stats_visible)

    def _open_dialog(self, name, widget):

        if name == 'Collapse Cube':
            ex = collapse_cube.CollapseCube(self._data, parent=self, allow_preview=True)

        if name == 'Spatial Smoothing':
            ex = smoothing.SelectSmoothing(self._data, parent=self, allow_preview=True)

        if name == 'Arithmetic Operations':
            ex = arithmetic_gui.SelectArithmetic(self._data, self.session.data_collection, parent=self)

        if name == "Moment Maps":
            mm_gui = moment_maps.MomentMapsGUI(
                self._data, self.session.data_collection, parent=self)
            mm_gui.display()

        if name == 'Wavelength Units':
            current_unit = self._units_controller.units_titles.index(self._units_controller._new_units.long_names[0].title())
            wavelength, ok_pressed = QInputDialog.getItem(self, "Pick a wavelength", "Wavelengths:", self._units_controller.units_titles, current_unit, False)
            if ok_pressed:
                self._units_controller.on_combobox_change(wavelength)

    def _toggle_all_coords_in_degrees(self):
        """
        Switch ra-dec b/w "Sexagesimal" and "Decimal Degrees"
        """
        menu = self.ra_dec_format_menu
        for action in menu.actions():
            if "Decimal Degrees" == action.text():
                coords_in_degrees = action.isChecked()
                break

        for view in self.cube_views:
            viewer = view.widget()
            if viewer._coords_in_degrees != coords_in_degrees:
                viewer.toggle_coords_in_degrees()

    @property
    def data_components(self):
        return self._data.main_components + self._data.derived_components

    @property
    def component_labels(self):
        return [str(cid) for cid in self.data_components]

    def refresh_viewer_combo_helpers(self):
        for i, helper in enumerate(self._viewer_combo_helpers):
            helper.refresh()

    @dispatch.register_listener("apply_operations")
    def apply_to_cube(self, stack):
        """
        Listen for messages from specviz about possible spectral analysis
        operations that may be applied to the entire cube.
        """

        # Retrieve the current cube data object
        operation_handler = SpectralOperationHandler(self._data,
                                                     stack=stack,
                                                     session=self.session,
                                                     parent=self)
        operation_handler.exec_()

    def remove_data_component(self, component_id):
        pass

    def _enable_option_buttons(self):
        for button in self._option_buttons:
            button.setEnabled(True)
        self.ui.sync_button.setEnabled(True)

    def _get_change_viewer_combo_func(self, combo, view_index):

        def _on_viewer_combo_change(dropdown_index):

            # This function gets called whenever one of the viewer combos gets
            # changed. The active combo is the one that comes from the parent
            # _get_change_viewer_combo_func function.

            # Find the relevant viewer
            viewer = self.cube_views[view_index].widget()

            # Get the label of the component and the component ID itself
            label = combo.currentText()
            component = combo.currentData()

            viewer.has_2d_data = component.parent[label].ndim == 2

            # If the user changed the current component, stop previewing
            # smoothing.
            if viewer.is_smoothing_preview_active:
                viewer.end_smoothing_preview()

            # Change the title and unit shown in the viwer
            viewer.update_component_unit_label(component)
            viewer.update_axes_title(title=str(label))

            # Change the viewer's reference data to be the data containing the
            # current component.
            viewer.state.reference_data = component.parent

            # The viewer may have multiple layers, for instance layers for
            # the main cube and for any overlay datasets, as well as subset
            # layers. We go through all the layers and make sure that for the
            # layer which corresponds to the current dataset, the correct
            # attribute is shown.
            for layer_artist in viewer.layers:
                layer_state = layer_artist.state
                if layer_state.layer is component.parent:

                    # We call _update_attribute here manually so that if this
                    # function gets called before _update_attribute, it gets
                    # called before we try and set the attribute below
                    # (_update_attribute basically updates the internal list
                    # of available attributes for the attribute combo)
                    layer_state._update_attribute()
                    layer_state.attribute = component

                    # We then also make sure that this layer artist is the
                    # one that is selected so that if the user uses e.g. the
                    # contrast tool, it will change the right layer
                    viewer._view.layer_list.select_artist(layer_artist)

            # If the combo corresponds to the currently active cube viewer,
            # either activate or deactivate the slice slider as appropriate.
            if self.cube_views[view_index] is self._active_cube:
                self._slice_controller.set_enabled(not viewer.has_2d_data)

            # If contours are being currently shown, we need to force a redraw
            if viewer.is_contour_active:
                viewer.draw_contour()

            viewer.update_component(component)

        return _on_viewer_combo_change

    def _enable_viewer_combo(self, data, index, combo_label, selection_label):
        combo = getattr(self.ui, combo_label)
        connect_combo_selection(self, selection_label, combo)
        helper = ComponentIDComboHelper(self, selection_label)
        helper.set_multiple_data([data])
        combo.setEnabled(True)
        combo.currentIndexChanged.connect(self._get_change_viewer_combo_func(combo, index))
        self._viewer_combo_helpers.append(helper)

    def _enable_all_viewer_combos(self, data):
        """
        Setup the dropdown boxes that correspond to each of the left, middle,
        and right views.  The combo boxes initially are set to have FLUX,
        Error, DQ but will be dynamic depending on the type of data available
        either from being loaded in or by being processed.

        :return:
        """
        self._enable_viewer_combo(
            data, 0, 'single_viewer_combo', 'single_viewer_attribute')
        view = self.cube_views[0].widget()
        component = getattr(self, 'single_viewer_attribute')
        view.update_component_unit_label(component)
        view.update_axes_title(component.label)

        for i in range(1,4):
            combo_label = 'viewer{0}_combo'.format(i)
            selection_label = 'viewer{0}_attribute'.format(i)
            self._enable_viewer_combo(data, i, combo_label, selection_label)
            view = self.cube_views[i].widget()
            component = getattr(self, selection_label)
            view.update_component_unit_label(component)
            view.update_axes_title(component.label)

    def change_viewer_component(self, view_index, component_id, force=False):
        """
        Given a viewer at an index view_index, change combo
        selection to component at an index component_index.
        :param view_index: int: Viewer index
        :param component_id: ComponentID: Component ID in viewer combo
        :param force: bool: force change if component is already displayed.
        """

        combo = self.get_viewer_combo(view_index)

        if isinstance(component_id, str):
            component_index = combo.findText(component_id)
        else:
            component_index = combo.findData(component_id)

        if combo.currentIndex() == component_index and force:
            combo.currentIndexChanged.emit(component_index)
        else:
            combo.setCurrentIndex(component_index)

    def display_component(self, component_id):
        """
        Displays data with given component ID in the active cube viewer.
        """
        self.refresh_viewer_combo_helpers()
        if self._single_viewer_mode:
            self.change_viewer_component(0, component_id)
        else:
            self.change_viewer_component(1, component_id)

    def get_viewer_combo(self, view_index):
        """
        Get viewer combo for a given viewer index
        """
        if view_index == 0:
            combo_label = 'single_viewer_combo'
        else:
            combo_label = 'viewer{0}_combo'.format(view_index)
        return getattr(self.ui, combo_label)

    def add_overlay(self, data, label, display_now=True):
        self._overlay_controller.add_overlay(data, label, display=display_now)
        self.display_component(label)

    def _set_data_coord_system(self, data):
        """
        Check if data coordinates are in
        RA-DEC first. Then set viewers to
        the default coordinate system.
        :param data: input data
        """
        is_ra_dec = isinstance(wcs_to_celestial_frame(data.coords.wcs),
                               BaseRADecFrame)
        self.ra_dec_format_menu.setDisabled(not is_ra_dec)
        if not is_ra_dec:
            return

        is_coords_in_degrees = False
        for view in self.cube_views:
            viewer = view.widget()
            viewer.init_ra_dec()
            is_coords_in_degrees = viewer._coords_in_degrees

        if is_coords_in_degrees:
            format_name = "Decimal Degrees"
        else:
            format_name = "Sexagesimal"

        menu = self.ra_dec_format_menu
        for action in menu.actions():
            if format_name == action.text():
                action.setChecked(True)
                break

    def add_data(self, data):
        """
        Called by a function outside the class in order to add data to cubeviz.

        :param data:
        :return:
        """
        self._data = data
        self.specviz._widget.add_data(data)
        cid = self.specviz._widget._options_widget.file_att
        dispatch.changed_units.emit(y=data.get_component(cid).units)

        for checkbox in self._synced_checkboxes:
            checkbox.setEnabled(True)

        self._has_data = True
        self._active_view = self.split_views[0]
        self._active_cube = self.split_views[0]
        self._last_active_view = self.single_view
        self._active_split_cube = self.split_views[0]

        # Store pointer to wavelength information
        self._wavelengths = self.single_view._widget._data[0].coords.world_axis(self.single_view._widget._data[0], axis=0)

        # Pass WCS and wavelength information to slider controller and enable
        wcs = self.session.data_collection.data[0].coords.wcs
        self._slice_controller.enable(wcs, self._wavelengths)
        self._units_controller.enable(wcs, self._wavelengths)

        self._enable_option_buttons()
        self._setup_syncing()

        self._enable_all_viewer_combos(data)

        self.subWindowActivated.emit(self._active_view)

        # Check if coord system is RA and DEC (ie not galactic etc..)
        self._set_data_coord_system(data)


    def eventFilter(self, obj, event):

        if event.type() == QtCore.QEvent.MouseButtonPress:

            if not (self.isVisible() and self.isActiveWindow()):
                return super(CubeVizLayout, self).eventFilter(obj, event)

            # Find global click position
            click_pos = event.globalPos()

            # If the click position is the same as the last one, we shouldn't
            # do anything.
            if click_pos != self._last_click:

                # Determine if the event falls inside any of the viewers
                for viewer in self.subWindowList():
                    relative_click_pos = viewer.mapFromGlobal(click_pos)
                    if viewer.rect().contains(relative_click_pos):
                        # We should only emit an event if the active subwindow
                        # has actually changed.
                        if viewer is not self.activeSubWindow():
                            self.subWindowActivated.emit(viewer)
                        break

                self._last_click = click_pos

        return super(CubeVizLayout, self).eventFilter(obj, event)

    def _toggle_image_mode(self, event=None):
        new_active_view = self._last_active_view
        self._last_active_view = self._active_view

        # Currently in single image, moving to split image
        if self._single_viewer_mode:
            self._active_cube = self._active_split_cube
            self._activate_split_image_mode(event)
            self._single_viewer_mode = False
            self.ui.button_toggle_image_mode.setText('Single Image Viewer')
            self.ui.viewer_control_frame.setCurrentIndex(0)

            for view in self.split_views:
                if self.single_view._widget.synced:
                    if view._widget.synced:
                        view._widget.update_slice_index(self.single_view._widget.slice_index)
                view._widget.update()
        # Currently in split image, moving to single image
        else:
            self._active_split_cube = self._active_cube
            self._active_view = self.single_view
            self._active_cube = self.single_view
            self._activate_single_image_mode(event)
            self._single_viewer_mode = True
            self.ui.button_toggle_image_mode.setText('Split Image Viewer')
            self.ui.viewer_control_frame.setCurrentIndex(1)
            self._active_view._widget.update()

        self.subWindowActivated.emit(new_active_view)

        # Update the slice index to reflect the state of the active cube
        self._slice_controller.update_index(self._active_cube._widget.slice_index)

    def _activate_single_image_mode(self, event=None):
        vsplitter = self.ui.vertical_splitter
        hsplitter = self.ui.horizontal_splitter
        vsizes = list(vsplitter.sizes())
        hsizes = list(hsplitter.sizes())
        vsizes = 0, max(10, vsizes[0] + vsizes[1])
        hsizes = max(10, sum(hsizes) * 0.4), max(10, sum(hsizes) * 0.6)
        vsplitter.setSizes(vsizes)
        hsplitter.setSizes(hsizes)

    def _activate_split_image_mode(self, event=None):
        vsplitter = self.ui.vertical_splitter
        hsplitter = self.ui.horizontal_splitter
        vsizes = list(vsplitter.sizes())
        hsizes = list(hsplitter.sizes())
        vsizes = max(10, sum(vsizes) / 2), max(10, sum(vsizes) / 2)

        # TODO:  Might be a bug here, should the hsizes be based on vsizes? If so, not sure we need to calculate
        # TODO:  the hsizes above.
        hsizes = 0, max(10, vsizes[0] + vsizes[1])
        vsplitter.setSizes(vsizes)
        hsplitter.setSizes(hsizes)

    def _update_active_view(self, view):
        if self._has_data:
            self._active_view = view
            if isinstance(view._widget, CubevizImageViewer):
                self._active_cube = view
                index = self._active_cube._widget.slice_index
                if view._widget.has_2d_data:
                    self._slice_controller.set_enabled(False)
                else:
                    self._slice_controller.set_enabled(True)
                    self._slice_controller.update_index(index)

    def activeSubWindow(self):
        return self._active_view

    def subWindowList(self):
        return self.cube_views + [self.specviz]

    def _setup_syncing(self):
        for attribute in ['x_min', 'x_max', 'y_min', 'y_max']:
            # TODO: this will need to be generalized if we want to support an
            # arbitrary number of viewers.
            sync1 = keep_in_sync(self.split_views[0]._widget.state, attribute,
                                 self.split_views[1]._widget.state, attribute)
            sync2 = keep_in_sync(self.split_views[1]._widget.state, attribute,
                                 self.split_views[2]._widget.state, attribute)
            self.sync[attribute] = sync1, sync2
        self._on_sync_click()

    def _on_sync_click(self, event=None):
        index = self._active_cube._widget.slice_index
        for view in self.cube_views:
            view._widget.synced = True
            if view != self._active_cube:
                view._widget.update_slice_index(index)
        self._slice_controller.update_index(index)

    def start_smoothing_preview(self, preview_function, component_id, preview_title=None):
        """
        Starts smoothing preview. This function preforms the following steps
        1) SelectSmoothing passes parameters.
        2) The left and single viewers' combo box is set to component_id
        3) The set_smoothing_preview is called to setup on the fly smoothing
        :param preview_function: function: Single-slice smoothing function
        :param component_id: int: Which component to preview
        :param preview_title: str: Title displayed when previewing
        """
        # For single and first viewer:
        self._original_components = {}
        for view_index in [0, 1]:
            combo = self.get_viewer_combo(view_index)
            self._original_components[view_index] = combo.currentData()
            view = self.cube_views[view_index].widget()
            self.change_viewer_component(view_index, component_id, force=True)
            view.set_smoothing_preview(preview_function, preview_title)

    def end_smoothing_preview(self):
        """
        End preview and change viewer combo index to the first component.
        """
        for view_index in [0, 1]:
            view = self.cube_views[view_index].widget()
            view.end_smoothing_preview()
            if view_index in self._original_components:
                component_id = self._original_components[view_index]
                self.change_viewer_component(view_index, component_id, force=True)
        self._original_components = {}

    def showEvent(self, event):
        super(CubeVizLayout, self).showEvent(event)
        # Make split image mode the default layout
        self._activate_split_image_mode()
        self._update_active_view(self.split_views[0])

    def change_slice_index(self, amount):
        self._slice_controller.change_slider_value(amount)

    def get_wavelengths(self):
        return self._wavelengths

    def get_wavelengths_units(self):
        return self._units_controller.get_new_units()

    def set_wavelengths(self, new_wavelengths, new_units):
        self._wavelengths = new_wavelengths
        self._slice_controller.set_wavelengths(new_wavelengths, new_units)
