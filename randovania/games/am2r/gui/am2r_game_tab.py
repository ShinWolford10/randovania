from __future__ import annotations

from randovania.games.game import RandovaniaGame
from randovania.gui.generated.games_tab_am2r_widget_ui import Ui_AM2RTabWidget
from randovania.gui.widgets.base_game_tab_widget import BaseGameTabWidget


class AM2RTabWidget(BaseGameTabWidget, Ui_AM2RTabWidget):
    def setup_ui(self):
        self.setupUi(self)

    @classmethod
    def game(cls) -> RandovaniaGame:
        return RandovaniaGame.AM2R
