from pydantic import BaseModel, Field


class PoomsaeLivestreamSettings(BaseModel):
    # OBS / window manager — same meaning as in the kyorugi extension.
    obs_collection: str = Field("Poomsae")
    i3_workspace: int = Field(1)
    obs_scene_starting: str = Field("Starting")
    obs_scene_main: str = Field("Main")
    obs_transition_move: str = Field("Move")
    # Athlete intro (name, flag, club — bottom-center) when Poomsae 1 of a
    # match starts, once per match. Seconds on screen; 0 = off.
    athlete_intro_seconds: int = Field(6, ge=0, le=60)
    # Show the Results widget (accuracy / presentation per judge) on
    # Results 1 / Results 2.
    show_results_widget: bool = Field(True)
    # Show the Final Score widget (P1 / P2 totals + overall) on Final Results.
    show_final_widget: bool = Field(True)
    # Category ranking on Category Results. Up to 5 athletes it sits at the
    # bottom like the other widgets; with more it is centred on screen.
    show_category_widget: bool = Field(True)
    # Longest list shown (top N). Keeps big categories on screen.
    category_max_rows: int = Field(16, ge=1, le=40)
    # Seconds after the athlete rows appear (every time the scoreboard shows
    # them) before names shorten to "O. M. Hlavinka" and the country code
    # hides. 0 = never.
    compact_after_seconds: int = Field(8, ge=0, le=120)
    # Seconds after the operator starts the timer before the clock row
    # collapses into the scoreboard. It comes back (with the round) on pause.
    # 0 = never collapse.
    clock_collapse_after_seconds: int = Field(5, ge=0, le=120)
    # A "-0.3" / "-0.1" is flashed only when a majority of the judges make
    # that same deduction within this many seconds of each other.
    deduction_window_seconds: float = Field(2.0, ge=0.3, le=10.0)