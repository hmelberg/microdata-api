"""Setningskoder for innlogging (askstat konto-runden, 2026-08-05): koden er
en KORT pseudo-setning på fem innholdsord uten fyllord — «brave otter kicked
golden drum» — fordi fyllord («the», «over») gir null entropi men lengre
tasting. Entropi = 2·log2(ADJ) + 2·log2(NOUN) + log2(VERB) = 40 bits med
256-ordslister (dagens 3 EFF-ord = 38,8). Listene brukes KUN ved generering —
koder lagres som hash, så listene kan endres fritt uten å brekke gamle koder.

Krav per liste: nøyaktig 256 unike ord, kun a-z (normalisereren dreper alt
annet), 3–9 tegn. Modul-asserts håndhever dette ved import (Anvil-oppstart
feiler høylytt fremfor å utstede svake koder i stillhet)."""

import re

_WORD_RE = re.compile(r"^[a-z]{3,9}$")


def _mklist(raw: str) -> tuple:
    words = tuple(sorted(set(raw.split())))
    assert len(words) >= 256, f"trenger minst 256 unike ord, fikk {len(words)}"
    bad = [w for w in words if not _WORD_RE.match(w)]
    assert not bad, f"ugyldige ord: {bad[:5]}"
    return words[:256]  # deterministisk (sortert) — nøyaktig 8 bits per slott


ADJECTIVES = _mklist("""
amber angry basic bitter black blue bold brave bright brown busy calm cheap
chilly clean clear clever cloudy cold cool cosy crazy crisp curly damp dark
deep dizzy dry dull dusty eager early easy empty even faint fair famous fancy
fast fat fierce fine firm flat fluffy fond formal frail free fresh full funny
fuzzy gentle giant glad glossy golden good grand gray green grim grumpy happy
hard hazy heavy hidden high hollow honest huge humble hungry icy idle itchy
jolly juicy keen kind large late lazy light little lively lonely long loose
loud loyal lucky mad mellow merry mighty mild misty modern moist narrow neat
nervous new nice noble noisy odd old orange pale patient plain playful polite
poor proud purple quick quiet rapid rare raw red rich ripe rough round royal
rusty sad safe salty sandy scarce shaky sharp shiny short shy silent silky
silver simple sleepy slim slow small smart smooth snowy soft solid sore sour
speedy spicy stale steep sticky stiff still stormy strange strict strong
sturdy sunny sweet swift tall tame tasty tender thick thin tidy tiny tired
tough tricky twin vague vast vivid warm wary weak weary wet white wide wild
windy wise witty wooden woolly young zesty able acid active actual added airy
alert alive ample ancient antique ashy awake aware bare beige blank bleak
blond bony bossy brief brisk broad bumpy burly candid casual chatty chief
chunky civil classy coarse comic common corny costly cozy creamy cruel cubic
curious curved cute daily dainty dandy dear decent dense dim direct double
drab dreamy dual due dusky earthy elder elegant
""")

NOUNS = _mklist("""
anchor apple arrow badger bakery balloon bamboo banana banjo barn basket bat
beach beacon bean bear beaver bell belt bench berry bison blanket boat bone
book boot bottle bowl box branch bread brick bridge broom brush bucket bug
bull bunny bus bush butter button cabin cactus camel camera canal candle
canoe cape car carpet carrot castle cat cave cellar chair chalk cheese
cherry chest chicken chimney church circle city clam cliff cloak clock cloud
clover coal coast coat cobra coin comet compass cook copper coral corn
cottage cotton cousin cow crab crane crayon cricket crow crown cup curtain
cushion daisy deer desk dice dog dolphin donkey door dragon drum duck eagle
earring eel engine falcon farm feather fence fern ferry field finch fire
fish flag flame flute fog forest fork fossil fountain fox frog garden gate
giraffe glacier glove goat goose grape grass guitar hammer hamster harbor
harp hat hawk hedge helmet heron hill hive honey hook horn horse hotel house
hut icicle igloo inn iron island ivory jacket jaguar jar jelly jewel jungle
kayak kettle key king kite kitten knife koala ladder lake lamp lantern leaf
lemon lentil leopard lily lion lizard llama lobster lock log loom lotus
magnet mango map maple marble market mask meadow melon mill mirror mitten
mole monkey moon moose moth mouse mule mushroom nest net newt oak oar ocean
octopus onion orchard otter owl oyster panda parrot peach pear pebble
pelican pencil penguin piano pigeon pillow pine pirate planet plum pond pony
puffin pumpkin puzzle rabbit raccoon radish raft rainbow raven reef ribbon
river robin rocket roof rose ruby saddle sailor salmon
""")

VERBS = _mklist("""
added aimed asked backed baked balanced banged batted beamed begged bent
blamed blessed blinked blocked bloomed blushed boiled boosted borrowed
bounced bowed boxed bragged braided braked braved brewed brought browsed
brushed built bumped burned buzzed called calmed camped carried carved
caught chained chanted charged chased checked cheered chewed chopped
chuckled circled claimed clapped cleaned cleared climbed clipped closed
coached coated coded collected colored combed cooked copied counted covered
cracked crafted crawled crossed crowned cruised crunched cuddled curled
cycled danced dared dashed dazzled decked decorated defended delivered
dialed dodged donated doodled dragged dreamed dressed dribbled dropped
drummed dusted earned echoed edited emailed escaped explored faced fanned
farmed fetched filed filled filmed fished fixed flapped flashed flipped
floated flowed folded followed formed fostered found framed fried frosted
gained galloped gathered gazed giggled glided glowed grabbed grazed greeted
grilled grinned gripped grouped guarded guessed guided handed hatched hauled
headed healed heard heated helped herded hiked hinted hopped hosted hugged
hummed hunted hurried iced invited ironed jogged joined joked juggled jumped
kicked kissed kneaded knitted knocked landed lasted laughed launched learned
leaned leaped lifted listed loaded locked logged looked looped lowered
mailed managed mapped marched marked mashed matched measured melted mended
mimed mixed moved mowed nailed named napped nodded noted noticed opened
ordered packed paddled painted parked passed pasted patted paused pecked
pedaled peeled phoned picked planned planted played pleased pledged plotted
plowed plucked pointed polished posted poured praised pressed printed pulled
pumped pushed raced raked reached rescued rested rinsed roamed roared
roasted rocked rolled rowed rubbed rushed sailed sampled saved scanned
scored scrubbed sealed searched served settled sewed shared shaved shifted
shipped shouted showed signed skated sketched skipped sliced smiled snapped
soared sorted spelled stacked stamped stirred stitched stopped stored
strolled strummed studied surfed swapped swayed swept swirled tagged talked
tamed tapped tasted thanked tied tilted toasted tossed touched towed traced
traded trained trimmed trotted tuned turned twirled typed visited voted
waded walked washed watched watered waved weighed whisked whistled winked
wiped wished worked wrapped yawned yelled zipped zoomed
""")


def generate_sentence_code(rng) -> str:
    """Kortest mulige setningskode: fem innholdsord, bindestrek-separert
    (URL-trygg kanonisk form; normalisereren gjør mellomrom ↔ bindestrek
    likeverdige). Eks: «brave-otter-kicked-golden-drum»."""
    return "-".join([
        rng.choice(ADJECTIVES),
        rng.choice(NOUNS),
        rng.choice(VERBS),
        rng.choice(ADJECTIVES),
        rng.choice(NOUNS),
    ])
